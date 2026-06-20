import asyncio
import hashlib
import json
import logging
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.request
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

import os

CONFIG = {
    "base_url": "https://www.scourt.go.kr/portal/notice/realestate/RealNoticeList.work",
    "detail_base_url": "https://www.scourt.go.kr/portal/notice/realestate/RealNoticeView.work",
    "file_base_url": "https://www.scourt.go.kr",
    "output_json": "scourt_data.json",
    "output_html": "scourt_viewer.html",
    "min_delay": 0.8,
    "max_delay": 2.0,
    "headless": True,
    "timeout": 30000,
    "port": int(os.environ.get("PORT", 8765)),
    "host": "0.0.0.0",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("법원경매서버")

crawl_status = {"running": False, "message": "대기 중", "last_updated": None, "total": 0}

# 비밀번호 설정
PASSWORD = os.environ.get("SITE_PASSWORD", "2027")
PASSWORD_HASH = hashlib.sha256(PASSWORD.encode()).hexdigest()
SESSIONS: set[str] = set()  # 유효한 세션 토큰


def 세션확인(handler) -> bool:
    """요청 쿠키에서 세션 토큰 확인"""
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("session="):
            token = part[len("session="):]
            if token in SESSIONS:
                return True
    return False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.scourt.go.kr/",
}


# ─── 크롤러 ───────────────────────────────────────────────

def 물건종별_추출(제목: str) -> str:
    if "자동차" in 제목: return "자동차"
    elif "선박" in 제목: return "선박"
    elif "항공기" in 제목: return "항공기"
    elif "건설기계" in 제목: return "건설기계"
    elif "유체동산" in 제목 or ("동산" in 제목 and "부동산" not in 제목): return "동산"
    elif "부동산" in 제목: return "부동산"
    elif "채권" in 제목: return "채권"
    elif "특허" in 제목 or "지식재산" in 제목: return "지식재산권"
    elif "주식" in 제목 or "지분" in 제목: return "주식·지분"
    elif "매각" in 제목 or "포기" in 제목: return "기타재산"
    else: return "기타"


async def 페이지_파싱(page) -> list:
    items = []
    try:
        rows = await page.query_selector_all("table tbody tr")
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) < 3:
                continue
            texts = [(await c.inner_text()).strip() for c in cells]
            if not any(texts):
                continue
            item = {"번호": "", "법원": "", "채무자": "", "제목": "", "조회수": "", "링크": "", "seq_id": "", "물건종별": ""}
            if len(texts) >= 4:
                item["번호"] = texts[0]
                item["법원"] = texts[1]
                item["채무자"] = texts[2]
                item["제목"] = texts[3]
                item["조회수"] = texts[4] if len(texts) > 4 else ""
            elif len(texts) == 3:
                item["번호"] = texts[0]
                item["법원"] = texts[1]
                item["제목"] = texts[2]

            link_el = await row.query_selector("a")
            if link_el:
                href = await link_el.get_attribute("href") or ""
                onclick = await link_el.get_attribute("onclick") or ""
                if href and href != "#":
                    item["링크"] = href if href.startswith("http") else f"https://www.scourt.go.kr{href}"
                    m = re.search(r"seq_id=(\d+)", href)
                    if m:
                        item["seq_id"] = m.group(1)
                elif onclick:
                    m = re.search(r"fn_view\('(\d+)'\)", onclick)
                    if m:
                        item["seq_id"] = m.group(1)
                        item["링크"] = f"{CONFIG['detail_base_url']}?seq_id={m.group(1)}"

            item["물건종별"] = 물건종별_추출(item["제목"])
            if item["제목"]:
                items.append(item)
    except Exception as e:
        logger.warning(f"파싱 오류: {e}")
    return items


async def 총페이지수_확인(page) -> int:
    try:
        paging = await page.query_selector(".paging, .pagination, #paging")
        if paging:
            text = await paging.inner_text()
            numbers = re.findall(r"\d+", text)
            if numbers:
                return max(int(n) for n in numbers if int(n) <= 200)
        last_btn = await page.query_selector("a[title='마지막 페이지'], a.last")
        if last_btn:
            for attr in ["onclick", "href"]:
                val = await last_btn.get_attribute(attr) or ""
                m = re.search(r"pageIndex=(\d+)", val)
                if m:
                    return int(m.group(1))
    except Exception as e:
        logger.warning(f"페이지 수 확인 오류: {e}")
    return 48


async def 크롤링():
    global crawl_status
    crawl_status["running"] = True
    crawl_status["message"] = "크롤링 시작..."
    모든_공고 = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=CONFIG["headless"])
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await context.new_page()
            page.set_default_timeout(CONFIG["timeout"])

            crawl_status["message"] = "1페이지 접속 중..."
            await page.goto(CONFIG["base_url"])
            await page.wait_for_load_state("networkidle")
            총페이지 = await 총페이지수_확인(page)

            items = await 페이지_파싱(page)
            모든_공고.extend(items)

            for page_num in range(2, 총페이지 + 1):
                await asyncio.sleep(random.uniform(CONFIG["min_delay"], CONFIG["max_delay"]))
                crawl_status["message"] = f"{page_num}/{총페이지} 페이지 수집 중... ({len(모든_공고)}건)"
                try:
                    await page.goto(f"{CONFIG['base_url']}?pageIndex={page_num}")
                    await page.wait_for_load_state("networkidle")
                    모든_공고.extend(await 페이지_파싱(page))
                except Exception as e:
                    logger.error(f"{page_num}페이지 오류: {e}")

            await browser.close()

        # 상세페이지에서 파일 정보 수집
        crawl_status["message"] = f"첨부파일 정보 수집 중... (0/{len(모든_공고)})"
        for i, item in enumerate(모든_공고):
            seq_id = item.get("seq_id")
            if seq_id:
                try:
                    result = 상세페이지_파싱(seq_id)
                    item["files"] = result.get("files", [])
                except Exception:
                    item["files"] = []
            else:
                item["files"] = []
            if i % 20 == 0:
                crawl_status["message"] = f"첨부파일 정보 수집 중... ({i}/{len(모든_공고)})"
            await asyncio.sleep(0.4)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = {"수집일시": now, "총건수": len(모든_공고), "공고목록": 모든_공고}
        Path(CONFIG["output_json"]).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(CONFIG["output_html"]).write_text(HTML_뷰어_생성(data), encoding="utf-8")
        crawl_status.update({"last_updated": now, "total": len(모든_공고), "message": f"완료: {len(모든_공고)}건 ({now})"})
        logger.info(f"크롤링 완료: {len(모든_공고)}건")
    except Exception as e:
        crawl_status["message"] = f"오류: {e}"
        logger.error(f"크롤링 오류: {e}")
    finally:
        crawl_status["running"] = False


def 크롤링_스레드():
    asyncio.run(크롤링())


# ─── 상세페이지 파싱 ──────────────────────────────────────
# 다운로드 메커니즘: POST https://file.scourt.go.kr/AttachDownload
#   form fields: file=서버파일명, path=011, downFile=표시파일명

FILE_DOWNLOAD_BASE = "https://file.scourt.go.kr/AttachDownload"
FILE_PATH_CODE = "011"
SOFFICE = r"C:\Program Files\LibreOffice\program\soffice.exe"


def 상세페이지_파싱(seq_id: str) -> dict:
    url = f"{CONFIG['detail_base_url']}?seq_id={seq_id}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read()
        # EUC-KR 우선 시도
        try:
            html = raw_bytes.decode("euc-kr")
        except Exception:
            html = raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": str(e), "content": "", "files": []}

    # 본문 텍스트 추출 (table 행들에서)
    content_parts = []
    for m in re.finditer(r'<td[^>]*>(.*?)</td>', html, re.DOTALL | re.IGNORECASE):
        cell = m.group(1)
        cell = re.sub(r'<br\s*/?>', '\n', cell, flags=re.IGNORECASE)
        cell = re.sub(r'<[^>]+>', '', cell)
        cell = cell.strip()
        if len(cell) > 10:
            content_parts.append(cell)
    content = "\n\n".join(content_parts[:20])

    # javascript:download('서버파일명','표시파일명') 패턴 추출
    files = []
    for m in re.finditer(
        r"javascript:download\(['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\)",
        html, re.IGNORECASE
    ):
        server_name = m.group(1)
        display_name = m.group(2)
        ext = server_name.rsplit(".", 1)[-1].lower() if "." in server_name else ""
        files.append({
            "name": display_name,
            "server_name": server_name,
            "ext": ext,
        })

    return {"content": content, "files": files}


# ─── 로그인 페이지 ───────────────────────────────────────

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>법원 경매공고 뷰어 - 로그인</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Malgun Gothic', sans-serif;
    background: linear-gradient(135deg, #1a3c5e 0%, #2d6a9f 100%);
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: #fff;
    border-radius: 16px;
    padding: 40px 36px;
    width: 340px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
    text-align: center;
  }
  .icon { font-size: 2.5rem; margin-bottom: 12px; }
  h1 { font-size: 1.2rem; color: #1a3c5e; margin-bottom: 6px; }
  p.sub { font-size: 0.8rem; color: #aaa; margin-bottom: 28px; }
  input[type=password] {
    width: 100%; padding: 12px 14px;
    border: 2px solid #e2e8f0; border-radius: 8px;
    font-size: 1.1rem; font-family: inherit;
    outline: none; text-align: center; letter-spacing: 0.3em;
    transition: border-color 0.2s;
  }
  input[type=password]:focus { border-color: #2d6a9f; }
  button {
    width: 100%; margin-top: 14px;
    padding: 12px; background: #2d6a9f; color: #fff;
    border: none; border-radius: 8px; font-size: 1rem;
    font-family: inherit; font-weight: 700; cursor: pointer;
    transition: background 0.2s;
  }
  button:hover { background: #1a3c5e; }
  <!--ERROR-->
</style>
</head>
<body>
<div class="card">
  <div class="icon">⚖️</div>
  <h1>회생·파산 자산매각 공고</h1>
  <p class="sub">접속하려면 비밀번호를 입력하세요</p>
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="비밀번호" autofocus />
    <button type="submit">입장</button>
  </form>
  <!--ERROR-->
</div>
</body>
</html>"""

# ─── HTTP 서버 ────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/login":
            body = LOGIN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 로그인 확인 (로그인 페이지 제외 모든 경로)
        if not 세션확인(self):
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
            return

        if path == "/" or path == "/index.html":
            html_path = Path(CONFIG["output_html"])
            if html_path.exists():
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"error": "viewer not found"}, 404)

        elif path == "/status":
            self.send_json(crawl_status)

        elif path == "/data":
            try:
                data = json.loads(Path(CONFIG["output_json"]).read_text(encoding="utf-8"))
                self.send_json(data)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/detail":
            seq_id = params.get("seq_id", [""])[0]
            if not seq_id:
                self.send_json({"error": "seq_id 필요"}, 400)
                return
            result = 상세페이지_파싱(seq_id)
            self.send_json(result)

        elif path == "/convert":
            # HWP → PDF 변환 후 반환
            server_name = params.get("file", [""])[0]
            display_name = params.get("name", [server_name])[0]
            if not server_name:
                self.send_json({"error": "file 파라미터 필요"}, 400)
                return
            try:
                # 1. HWP 다운로드
                form_data = urllib.parse.urlencode({
                    "file": server_name, "path": FILE_PATH_CODE, "downFile": display_name,
                }).encode("utf-8")
                req = urllib.request.Request(
                    FILE_DOWNLOAD_BASE, data=form_data,
                    headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    hwp_data = resp.read()

                # 2. 임시 파일에 저장 후 LibreOffice로 PDF 변환
                with tempfile.TemporaryDirectory() as tmpdir:
                    ext = server_name.rsplit(".", 1)[-1].lower()
                    hwp_path = Path(tmpdir) / f"input.{ext}"
                    hwp_path.write_bytes(hwp_data)
                    result = subprocess.run(
                        [SOFFICE, "--headless", "--convert-to", "pdf", "--outdir", tmpdir, str(hwp_path)],
                        capture_output=True, timeout=60
                    )
                    pdf_path = hwp_path.with_suffix(".pdf")
                    if not pdf_path.exists():
                        self.send_json({"error": f"변환 실패: {result.stderr.decode('utf-8','replace')}"}, 500)
                        return
                    pdf_data = pdf_path.read_bytes()

                safe_name = urllib.parse.quote(display_name.rsplit(".", 1)[0] + ".pdf")
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(pdf_data)))
                self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{safe_name}")
                self.end_headers()
                self.wfile.write(pdf_data)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/download":
            # 파일 다운로드 프록시: POST to file.scourt.go.kr/AttachDownload
            server_name = params.get("file", [""])[0]
            display_name = params.get("name", [server_name])[0]
            if not server_name:
                self.send_json({"error": "file 파라미터 필요"}, 400)
                return
            try:
                form_data = urllib.parse.urlencode({
                    "file": server_name,
                    "path": FILE_PATH_CODE,
                    "downFile": display_name,
                }).encode("utf-8")
                req = urllib.request.Request(
                    FILE_DOWNLOAD_BASE,
                    data=form_data,
                    headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    file_data = resp.read()
                ext = server_name.rsplit(".", 1)[-1].lower()
                safe_name = urllib.parse.quote(display_name)
                # 확장자 기반으로 Content-Type 강제 지정
                mime_map = {
                    "pdf": "application/pdf",
                    "hwp": "application/x-hwp",
                    "hwpx": "application/x-hwp",
                    "doc": "application/msword",
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "xls": "application/vnd.ms-excel",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                }
                content_type = mime_map.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(file_data)))
                if ext == "pdf":
                    self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{safe_name}")
                else:
                    self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{safe_name}")
                self.end_headers()
                self.wfile.write(file_data)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            params = dict(urllib.parse.parse_qsl(body))
            pw = params.get("password", "")
            if hashlib.sha256(pw.encode()).hexdigest() == PASSWORD_HASH:
                token = secrets.token_hex(32)
                SESSIONS.add(token)
                self.send_response(302)
                self.send_header("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                body = LOGIN_PAGE.replace("<!--ERROR-->",
                    '<p style="color:#e53e3e;margin-top:12px;font-size:0.9rem;">비밀번호가 틀렸습니다.</p>'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        if not 세션확인(self):
            self.send_json({"error": "unauthorized"}, 401)
            return

        if path == "/refresh":
            if crawl_status["running"]:
                self.send_json({"ok": False, "error": "이미 크롤링 중입니다"})
                return
            threading.Thread(target=크롤링_스레드, daemon=True).start()
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "not found"}, 404)


# ─── HTML 뷰어 생성 ───────────────────────────────────────

def HTML_뷰어_생성(data: dict) -> str:
    items = data["공고목록"]
    수집일시 = data["수집일시"]
    총건수 = data["총건수"]

    법원_카운트: dict = {}
    for i in items:
        c = (i.get("법원", "") or "").strip()
        if c:
            법원_카운트[c] = 법원_카운트.get(c, 0) + 1
    법원_목록 = sorted(법원_카운트.keys(), key=lambda x: -법원_카운트[x])

    종별_카운트: dict = {}
    for i in items:
        t = i.get("물건종별", "기타")
        종별_카운트[t] = 종별_카운트.get(t, 0) + 1
    종별_목록 = sorted(종별_카운트.keys(), key=lambda x: -종별_카운트[x])

    items_json = json.dumps(items, ensure_ascii=False)
    court_options = "".join(f'<option value="{c}">{c} ({법원_카운트[c]})</option>' for c in 법원_목록)
    type_options = "".join(f'<option value="{t}">{t} ({종별_카운트[t]})</option>' for t in 종별_목록)
    port = CONFIG["port"]

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>법원 회생·파산 자산매각 공고 뷰어</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Malgun Gothic', sans-serif; background: #f5f6fa; color: #333; }}
  header {{
    background: linear-gradient(135deg, #1a3c5e 0%, #2d6a9f 100%);
    color: #fff; padding: 18px 32px;
    display: flex; justify-content: space-between; align-items: center;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
  }}
  header h1 {{ font-size: 1.35rem; font-weight: 700; }}
  .header-right {{ display: flex; align-items: center; gap: 16px; }}
  .meta {{ font-size: 0.78rem; opacity: 0.85; text-align: right; line-height: 1.7; }}
  .btn-refresh {{
    padding: 9px 20px; background: #fff; color: #1a3c5e;
    border: none; border-radius: 8px; cursor: pointer;
    font-size: 0.88rem; font-family: inherit; font-weight: 700;
    display: flex; align-items: center; gap: 6px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.15); white-space: nowrap;
    transition: all 0.2s;
  }}
  .btn-refresh:hover {{ background: #e8f0fe; transform: translateY(-1px); }}
  .btn-refresh:disabled {{ opacity: 0.5; cursor: not-allowed; transform: none; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .spinning {{ display: inline-block; animation: spin 1s linear infinite; }}
  .toast {{
    position: fixed; bottom: 24px; right: 24px;
    background: #1a3c5e; color: #fff;
    padding: 12px 20px; border-radius: 10px;
    font-size: 0.88rem; box-shadow: 0 4px 16px rgba(0,0,0,0.25);
    display: none; z-index: 9999; max-width: 360px; line-height: 1.5;
  }}
  .toast.show {{ display: block; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 24px 16px; }}
  .filter-panel {{
    background: #fff; border-radius: 12px;
    padding: 18px 22px; margin-bottom: 18px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  }}
  .filter-row {{ display: flex; gap: 14px; flex-wrap: wrap; align-items: flex-end; }}
  .filter-group {{ display: flex; flex-direction: column; gap: 5px; }}
  .filter-group label {{ font-size: 0.76rem; font-weight: 600; color: #555; }}
  .filter-group select, .filter-group input {{
    border: 1px solid #ddd; border-radius: 6px;
    padding: 7px 10px; font-size: 0.88rem;
    font-family: inherit; outline: none; transition: border-color 0.2s;
  }}
  .filter-group select:focus, .filter-group input:focus {{ border-color: #2d6a9f; }}
  .filter-group input {{ min-width: 210px; }}
  .btn-reset {{
    padding: 7px 16px; background: #6c757d; color: #fff;
    border: none; border-radius: 6px; cursor: pointer;
    font-size: 0.88rem; font-family: inherit;
  }}
  .btn-reset:hover {{ background: #545b62; }}
  .tabs {{ display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 14px; }}
  .tab-btn {{
    padding: 6px 13px; border: 2px solid #ddd;
    background: #fff; border-radius: 20px; cursor: pointer;
    font-size: 0.8rem; font-family: inherit;
    transition: all 0.2s; white-space: nowrap;
  }}
  .tab-btn:hover {{ border-color: #2d6a9f; color: #2d6a9f; }}
  .tab-btn.active {{ background: #2d6a9f; color: #fff; border-color: #2d6a9f; }}
  .tab-btn .cnt {{
    display: inline-block; background: rgba(255,255,255,0.3);
    border-radius: 10px; padding: 1px 5px; font-size: 0.7rem; margin-left: 3px;
  }}
  .tab-btn:not(.active) .cnt {{ background: #e9ecef; color: #555; }}
  .result-summary {{ font-size: 0.86rem; color: #666; margin-bottom: 10px; padding: 0 2px; }}
  .result-summary strong {{ color: #2d6a9f; }}
  .table-wrap {{
    background: #fff; border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08); overflow: hidden;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  thead th {{
    background: #1a3c5e; color: #fff;
    padding: 11px 14px; text-align: left;
    font-size: 0.8rem; font-weight: 600; white-space: nowrap;
  }}
  /* 데이터 행 */
  tr.data-row {{ cursor: pointer; transition: background 0.12s; }}
  tr.data-row:hover {{ background: #eef4ff !important; }}
  tr.data-row.open {{ background: #e8f0fe !important; }}
  tr.data-row:nth-child(4n+1), tr.data-row:nth-child(4n+2) {{ background: #fafbfc; }}
  td {{ padding: 10px 14px; font-size: 0.86rem; border-bottom: 1px solid #f0f0f0; vertical-align: middle; }}
  td.num {{ color: #bbb; font-size: 0.76rem; text-align: center; width: 52px; }}
  td.court {{ font-weight: 700; color: #1a3c5e; white-space: nowrap; width: 125px; font-size: 0.8rem; }}
  td.title {{ color: #222; }}
  td.debtor {{ color: #777; font-size: 0.78rem; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  td.toggle-arrow {{ width: 28px; text-align: center; color: #aaa; font-size: 0.8rem; transition: transform 0.2s; }}
  tr.data-row.open td.toggle-arrow {{ color: #2d6a9f; }}
  .badge {{ display: inline-block; padding: 2px 9px; border-radius: 10px; font-size: 0.72rem; font-weight: 700; }}
  .b-동산 {{ background:#d4f5e2; color:#1a7a45; }}
  .b-기타재산 {{ background:#fff3cd; color:#856404; }}
  .b-채권 {{ background:#cfe2ff; color:#0a58ca; }}
  .b-부동산 {{ background:#d4edff; color:#0066cc; }}
  .b-자동차 {{ background:#ffe4b5; color:#cc6600; }}
  .b-선박 {{ background:#e8d5f5; color:#6b21a8; }}
  .b-항공기 {{ background:#ffe4e6; color:#9f1239; }}
  .b-건설기계 {{ background:#fde8d4; color:#9a3412; }}
  .b-지식재산권 {{ background:#e0f2fe; color:#0369a1; }}
  .b-주식지분 {{ background:#f3e8ff; color:#7c3aed; }}
  .b-기타 {{ background:#e9ecef; color:#555; }}
  td.views {{ color: #bbb; font-size: 0.76rem; text-align: right; width: 52px; }}

  /* 아코디언 상세 행 */
  tr.detail-row td {{ padding: 0; border-bottom: 2px solid #2d6a9f; background: #f7f9ff; }}
  .detail-panel {{
    padding: 20px 24px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }}
  @media (max-width: 900px) {{ .detail-panel {{ grid-template-columns: 1fr; }} }}
  .detail-left, .detail-right {{ display: flex; flex-direction: column; gap: 12px; }}
  .detail-section {{ background: #fff; border-radius: 8px; padding: 14px 16px; border: 1px solid #e2e8f0; }}
  .detail-section h4 {{ font-size: 0.78rem; font-weight: 700; color: #2d6a9f; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .detail-content {{
    font-size: 0.84rem; color: #444; line-height: 1.7;
    white-space: pre-wrap; max-height: 200px; overflow-y: auto;
  }}
  .detail-loading {{ color: #aaa; font-size: 0.85rem; text-align: center; padding: 20px; }}
  /* 파일 목록 */
  .file-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .file-item {{
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px; border-radius: 6px; border: 1px solid #e2e8f0;
    background: #fafbfc;
  }}
  .file-icon {{ font-size: 1.1rem; flex-shrink: 0; }}
  .file-name {{ font-size: 0.82rem; color: #333; flex: 1; word-break: break-all; }}
  .file-btns {{ display: flex; gap: 6px; flex-shrink: 0; }}
  .btn-preview, .btn-download {{
    padding: 4px 10px; border-radius: 5px; border: none;
    font-size: 0.76rem; font-family: inherit; cursor: pointer; font-weight: 600;
  }}
  .btn-preview {{ background: #2d6a9f; color: #fff; }}
  .btn-preview:hover {{ background: #1a3c5e; }}
  .btn-download {{ background: #e9ecef; color: #333; }}
  .btn-download:hover {{ background: #dee2e6; }}
  /* PDF 미리보기 */
  .pdf-viewer {{
    width: 100%; height: 480px; border: none; border-radius: 6px;
    background: #eee;
  }}
  .hwp-notice {{
    padding: 14px; background: #fff8e1; border-radius: 6px;
    font-size: 0.82rem; color: #856404; border: 1px solid #ffe082;
  }}
  .no-file {{ font-size: 0.82rem; color: #aaa; padding: 8px 0; }}

  .empty {{ text-align: center; padding: 60px; color: #aaa; }}
  .pagination {{ display: flex; justify-content: center; gap: 5px; padding: 18px 0; flex-wrap: wrap; }}
  .page-btn {{
    padding: 5px 11px; border: 1px solid #ddd;
    background: #fff; border-radius: 6px;
    cursor: pointer; font-size: 0.82rem; font-family: inherit;
  }}
  .page-btn:hover {{ border-color: #2d6a9f; color: #2d6a9f; }}
  .page-btn.active {{ background: #2d6a9f; color: #fff; border-color: #2d6a9f; }}
  .page-btn:disabled {{ opacity: 0.35; cursor: default; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>&#9878; 회생·파산 자산매각 공고 뷰어</h1>
    <div style="font-size:0.8rem;opacity:0.8;margin-top:3px;">대한민국 법원 회생·파산 자산매각안내게시판</div>
  </div>
  <div class="header-right">
    <div class="meta" id="metaInfo">수집: {수집일시}<br>총 <strong style="font-size:1.05rem">{총건수:,}</strong>건</div>
    <button class="btn-refresh" id="btnRefresh" onclick="startRefresh()">
      <span id="refreshIcon">&#x21bb;</span> 새로고침
    </button>
  </div>
</header>
<div class="toast" id="toast"></div>

<div class="container">
  <div class="filter-panel">
    <div class="filter-row">
      <div class="filter-group">
        <label>법원별</label>
        <select id="selCourt" onchange="applyFilter()">
          <option value="">전체 법원</option>
          {court_options}
        </select>
      </div>
      <div class="filter-group">
        <label>물건종별</label>
        <select id="selType" onchange="applyFilter()">
          <option value="">전체 종별</option>
          {type_options}
        </select>
      </div>
      <div class="filter-group">
        <label>검색어</label>
        <input type="text" id="searchWord" placeholder="제목/채무자 검색..." oninput="applyFilter()" />
      </div>
      <div class="filter-group">
        <label>&nbsp;</label>
        <button class="btn-reset" onclick="resetFilter()">초기화</button>
      </div>
    </div>
  </div>
  <div style="font-size:0.74rem;font-weight:700;color:#888;letter-spacing:0.05em;margin-bottom:6px;">법원별</div>
  <div class="tabs" id="courtTabs"></div>
  <div style="font-size:0.74rem;font-weight:700;color:#888;letter-spacing:0.05em;margin-bottom:6px;margin-top:10px;">종별</div>
  <div class="tabs" id="typeTabs"></div>
  <div class="result-summary" id="resultSummary" style="margin-top:12px;"></div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th style="text-align:center">번호</th>
          <th>법원</th>
          <th>공고 내용</th>
          <th>채무자 정보</th>
          <th>종별</th>
          <th style="text-align:right">조회</th>
          <th style="width:28px"></th>
        </tr>
      </thead>
      <tbody id="tableBody"></tbody>
    </table>
    <div class="empty" id="emptyMsg" style="display:none">검색 결과가 없습니다.</div>
  </div>
  <div class="pagination" id="pagination"></div>
</div>

<script>
let DATA = {items_json};
const PER_PAGE = 20;
const API = window.location.origin === 'null' ? 'http://localhost:{port}' : '';
let curPage = 1;
let filtered = [...DATA];
let openRow = null; // 현재 열린 행 인덱스

function buildTabs() {{
  // 법원 탭
  const courtCnt = {{}};
  DATA.forEach(d => {{ const c=(d['법원']||'').trim(); if(c) courtCnt[c]=(courtCnt[c]||0)+1; }});
  const courts = Object.entries(courtCnt).sort((a,b)=>b[1]-a[1]).map(e=>e[0]);
  const courtCon = document.getElementById('courtTabs');
  courtCon.innerHTML = '';
  const allCourt = mkTab('전체', DATA.length, '', 'court');
  allCourt.classList.add('active');
  courtCon.appendChild(allCourt);
  courts.forEach(c => courtCon.appendChild(mkTab(c, courtCnt[c], c, 'court')));

  // 종별 탭
  const typeCnt = {{}};
  DATA.forEach(d => {{ const t=d['물건종별']||'기타'; typeCnt[t]=(typeCnt[t]||0)+1; }});
  const types = Object.entries(typeCnt).sort((a,b)=>b[1]-a[1]).map(e=>e[0]);
  const typeCon = document.getElementById('typeTabs');
  typeCon.innerHTML = '';
  const allType = mkTab('전체', DATA.length, '', 'type');
  allType.classList.add('active');
  typeCon.appendChild(allType);
  types.forEach(t => typeCon.appendChild(mkTab(t, typeCnt[t], t, 'type')));
}}

function mkTab(label, count, val, group) {{
  const btn = document.createElement('button');
  btn.className = 'tab-btn';
  btn.dataset.val = val;
  btn.dataset.group = group;
  btn.innerHTML = label + ' <span class="cnt">' + count + '</span>';
  btn.onclick = function() {{
    document.querySelectorAll('.tab-btn[data-group="'+group+'"]').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    if (group === 'court') document.getElementById('selCourt').value = val;
    else document.getElementById('selType').value = val;
    curPage = 1; openRow = null;
    applyFilter();
  }};
  return btn;
}}

function applyFilter() {{
  const court = document.getElementById('selCourt').value;
  const type = document.getElementById('selType').value;
  const kw = document.getElementById('searchWord').value.trim().toLowerCase();
  // 탭 동기화
  document.querySelectorAll('.tab-btn[data-group="court"]').forEach(b => b.classList.toggle('active', b.dataset.val === court));
  document.querySelectorAll('.tab-btn[data-group="type"]').forEach(b => b.classList.toggle('active', b.dataset.val === type));
  filtered = DATA.filter(function(d) {{
    if (court && (d['법원']||'').trim() !== court) return false;
    if (type && d['물건종별'] !== type) return false;
    if (kw && !(d['제목']||'').toLowerCase().includes(kw) && !(d['채무자']||'').toLowerCase().includes(kw)) return false;
    return true;
  }});
  curPage = 1; openRow = null;
  render();
}}

function resetFilter() {{
  document.getElementById('selCourt').value = '';
  document.getElementById('selType').value = '';
  document.getElementById('searchWord').value = '';
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.val === ''));
  filtered = [...DATA]; curPage = 1; openRow = null;
  render();
}}

function badgeClass(t) {{ return 'badge b-' + (t||'기타').replace(/[·]/g,''); }}

function render() {{
  const tbody = document.getElementById('tableBody');
  const empty = document.getElementById('emptyMsg');
  const summary = document.getElementById('resultSummary');
  const start = (curPage-1)*PER_PAGE, end = start+PER_PAGE;
  const pageData = filtered.slice(start, end);
  summary.innerHTML = '총 <strong>' + filtered.length.toLocaleString() + '</strong>건 &nbsp;|&nbsp; ' +
    (start+1) + '~' + Math.min(end, filtered.length) + '번째 표시 &nbsp;<small style="color:#aaa">※ 행 클릭 시 상세보기</small>';
  if (!filtered.length) {{ tbody.innerHTML=''; empty.style.display='block'; renderPaging(); return; }}
  empty.style.display = 'none';
  tbody.innerHTML = pageData.map(function(d, i) {{
    const badge = '<span class="'+badgeClass(d['물건종별'])+'">'+(d['물건종별']||'기타')+'</span>';
    const debtor = d['채무자']||'';
    const short = debtor.length>36 ? debtor.slice(0,36)+'…' : (debtor||'-');
    return '<tr class="data-row" data-idx="'+i+'" data-seqid="'+(d['seq_id']||'')+'" onclick="toggleRow(this, '+i+')">' +
      '<td class="num">'+(d['번호']||'')+'</td>' +
      '<td class="court">'+(d['법원']||'-')+'</td>' +
      '<td class="title">'+(d['제목']||'-')+'</td>' +
      '<td class="debtor" title="'+debtor+'">'+short+'</td>' +
      '<td>'+badge+'</td>' +
      '<td class="views">'+(d['조회수']||'-')+'</td>' +
      '<td class="toggle-arrow">&#9660;</td>' +
      '</tr>';
  }}).join('');
  renderPaging();
}}

function toggleRow(tr, idx) {{
  // 같은 행 다시 클릭 → 닫기
  const existingDetail = tr.nextElementSibling;
  if (existingDetail && existingDetail.classList.contains('detail-row')) {{
    existingDetail.remove();
    tr.classList.remove('open');
    const arrow = tr.querySelector('.toggle-arrow');
    if (arrow) arrow.innerHTML = '&#9660;';
    openRow = null;
    return;
  }}
  // 다른 열린 행 닫기
  const prev = document.querySelector('tr.data-row.open');
  if (prev) {{
    const prevDetail = prev.nextElementSibling;
    if (prevDetail && prevDetail.classList.contains('detail-row')) prevDetail.remove();
    prev.classList.remove('open');
    const prevArrow = prev.querySelector('.toggle-arrow');
    if (prevArrow) prevArrow.innerHTML = '&#9660;';
  }}
  // 새 행 열기
  tr.classList.add('open');
  const arrow = tr.querySelector('.toggle-arrow');
  if (arrow) arrow.innerHTML = '&#9650;';
  openRow = idx;

  const detailTr = document.createElement('tr');
  detailTr.className = 'detail-row';
  const seqId = tr.dataset.seqid;
  detailTr.innerHTML = '<td colspan="7"><div class="detail-panel"><div class="detail-loading">상세 정보를 불러오는 중...</div></div></td>';
  tr.after(detailTr);

  if (!seqId) {{
    detailTr.querySelector('.detail-panel').innerHTML = '<div style="padding:16px;color:#aaa;">상세 링크 정보가 없습니다.</div>';
    return;
  }}

  // 크롤링 시 저장된 파일 정보가 있으면 API 호출 없이 바로 표시
  const pageStart = (curPage-1)*PER_PAGE;
  const itemData = filtered[pageStart + idx];
  if (itemData && Array.isArray(itemData.files)) {{
    renderDetail(detailTr, {{content: '', files: itemData.files}}, seqId);
    return;
  }}

  fetch(API + '/detail?seq_id=' + seqId)
    .then(r => r.json())
    .then(res => {{ renderDetail(detailTr, res, seqId); }})
    .catch(e => {{
      detailTr.querySelector('.detail-panel').innerHTML =
        '<div style="padding:16px;color:#e53e3e;">서버 연결 오류. scourt_server.py를 실행해주세요.<br><small>' + e + '</small></div>';
    }});
}}

function dlUrl(f) {{
  return API + '/download?file=' + encodeURIComponent(f.server_name) + '&name=' + encodeURIComponent(f.name);
}}

function convertUrl(f) {{
  return API + '/convert?file=' + encodeURIComponent(f.server_name) + '&name=' + encodeURIComponent(f.name);
}}

function directDownload(serverName, displayName) {{
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = 'https://file.scourt.go.kr/AttachDownload';
  form.target = '_blank';
  form.style.display = 'none';
  const f1 = document.createElement('input'); f1.name='file'; f1.value=serverName;
  const f2 = document.createElement('input'); f2.name='path'; f2.value='011';
  const f3 = document.createElement('input'); f3.name='downFile'; f3.value=displayName;
  form.append(f1, f2, f3);
  document.body.appendChild(form);
  form.submit();
  setTimeout(()=>form.remove(), 1000);
}}

function renderDetail(detailTr, res, seqId) {{
  const files = res.files || [];
  const content = res.content || '';
  const pdfFiles = files.filter(f => f.ext === 'pdf');
  const otherFiles = files.filter(f => f.ext !== 'pdf');
  const detailUrl = 'https://www.scourt.go.kr/portal/notice/realestate/RealNoticeView.work?seq_id=' + seqId;

  let leftHtml = '';
  if (content) {{
    leftHtml += '<div class="detail-section"><h4>공고 내용</h4><div class="detail-content">' + escHtml(content) + '</div></div>';
  }}
  if (files.length) {{
    leftHtml += '<div class="detail-section"><h4>첨부파일</h4><div class="file-list">' +
      files.map(f => fileItem(f)).join('') + '</div></div>';
  }}
  leftHtml += '<div class="detail-section" style="margin-top:4px">' +
    '<a href="' + detailUrl + '" target="_blank" style="font-size:0.82rem;color:#2d6a9f;">&#128279; 법원 원문 페이지 열기</a></div>';
  if (!leftHtml) leftHtml = '<div class="detail-section"><p class="no-file">정보 없음</p></div>';

  let rightHtml = '';
  // PDF + HWP 모두 미리보기 대상
  const previewFiles = files.filter(f => f.ext === 'pdf' || f.ext === 'hwp' || f.ext === 'hwpx');
  if (previewFiles.length) {{
    const iframeId = 'pdf-' + seqId;
    rightHtml = '<div class="detail-section"><h4>미리보기</h4>' +
      previewFiles.map((f, i) => {{
        const fid = iframeId + '-' + i;
        const isHwp = f.ext === 'hwp' || f.ext === 'hwpx';
        const label = isHwp ? '&#128209; ' + escHtml(f.name) + ' <small style="color:#856404">(HWP→PDF 변환 중...)</small>'
                            : '&#128196; ' + escHtml(f.name);
        return '<div style="margin-bottom:12px">' +
          '<div style="font-size:0.78rem;color:#555;margin-bottom:5px;">' + label + '</div>' +
          '<div id="wrap-'+fid+'" style="width:100%;height:500px;background:#f0f0f0;border-radius:6px;display:flex;align-items:center;justify-content:center;">' +
          '<span style="color:#aaa;font-size:0.85rem;">&#8987; 로딩 중...</span></div></div>';
      }}).join('') + '</div>';

    setTimeout(function() {{
      previewFiles.forEach(function(f, i) {{
        const fid = iframeId + '-' + i;
        const wrap = document.getElementById('wrap-' + fid);
        if (!wrap) return;
        const isHwp = f.ext === 'hwp' || f.ext === 'hwpx';
        if (isHwp) {{
          // HWP: 다운로드 버튼만 제공
          wrap.style.height = 'auto';
          wrap.innerHTML = '<div style="padding:16px;color:#856404;font-size:0.85rem;">HWP 파일은 미리보기 불가 - 다운로드 후 확인하세요.<br>' +
            '<button onclick="directDownload(\''+f.server_name.replace(/'/g,"\\'") +'\',\''+f.name.replace(/'/g,"\\'")+'\');" style="margin-top:8px;padding:6px 14px;background:#e8a317;color:#fff;border:none;border-radius:6px;cursor:pointer;">&#11015; HWP 다운로드</button></div>';
          return;
        }}
        // PDF: 로컬 서버가 있으면 프록시, 없으면 iframe 직접 로드 시도
        const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
        if (isLocal) {{
          fetch(dlUrl(f))
            .then(r => {{ if (!r.ok) throw new Error(r.status); return r.blob(); }})
            .then(blob => {{
              const url = URL.createObjectURL(blob);
              wrap.innerHTML = '<iframe src="' + url + '#toolbar=1" style="width:100%;height:100%;border:none;border-radius:6px;"></iframe>';
            }})
            .catch(err => {{
              wrap.innerHTML = '<div style="padding:16px;color:#e53e3e;font-size:0.82rem;">미리보기 실패: ' + err + '</div>';
            }});
        }} else {{
          // Render 환경: iframe에 form POST로 직접 로드
          const fname = 'pf_' + fid.replace(/-/g,'_');
          wrap.innerHTML = '<iframe name="' + fname + '" style="width:100%;height:100%;border:none;border-radius:6px;"></iframe>';
          const form = document.createElement('form');
          form.method='POST'; form.action='https://file.scourt.go.kr/AttachDownload';
          form.target=fname; form.style.display='none';
          const i1=document.createElement('input'); i1.name='file'; i1.value=f.server_name;
          const i2=document.createElement('input'); i2.name='path'; i2.value='011';
          const i3=document.createElement('input'); i3.name='downFile'; i3.value=f.name;
          form.append(i1,i2,i3);
          document.body.appendChild(form);
          form.submit();
          setTimeout(()=>form.remove(),1000);
          // X-Frame-Options 차단 시 fallback 다운로드 버튼 표시
          setTimeout(function() {{
            try {{
              const iw = wrap.querySelector('iframe');
              if (iw && (!iw.contentDocument || iw.contentDocument.body.innerHTML === '')) {{
                wrap.style.height='auto';
                wrap.innerHTML='<div style="padding:16px;color:#555;font-size:0.85rem;">iframe 미리보기 차단됨.<br>' +
                  '<button onclick="directDownload(\''+f.server_name.replace(/'/g,"\\'")+'\',\''+f.name.replace(/'/g,"\\'")+'\');" style="margin-top:8px;padding:6px 14px;background:#2d6a9f;color:#fff;border:none;border-radius:6px;cursor:pointer;">&#11015; PDF 다운로드</button></div>';
              }}
            }} catch(e) {{}}
          }}, 3000);
        }}
      }});
    }}, 50);
  }} else {{
    rightHtml = '<div class="detail-section" style="color:#aaa;font-size:0.84rem;">미리보기 가능한 파일 없음</div>';
  }}

  detailTr.querySelector('.detail-panel').innerHTML =
    '<div class="detail-left">' + leftHtml + '</div>' +
    '<div class="detail-right">' + rightHtml + '</div>';
}}

function fileItem(f) {{
  const icons = {{pdf:'&#128196;', hwp:'&#128209;', hwpx:'&#128209;', doc:'&#128196;', docx:'&#128196;'}};
  const icon = icons[f.ext] || '&#128190;';
  const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
  const sn = f.server_name.replace(/'/g,"\\'"), dn = f.name.replace(/'/g,"\\'");
  const dlBtn = isLocal
    ? '<a class="btn-download" href="' + dlUrl(f) + '" download="' + escHtml(f.name) + '">&#11015; 다운로드</a>'
    : '<button class="btn-download" onclick="directDownload(\''+sn+'\',\''+dn+'\');">&#11015; 다운로드</button>';
  return '<div class="file-item">' +
    '<span class="file-icon">' + icon + '</span>' +
    '<span class="file-name">' + escHtml(f.name) + '</span>' +
    '<div class="file-btns">' + dlBtn + '</div></div>';
}}

function escHtml(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function renderPaging() {{
  const total = Math.ceil(filtered.length / PER_PAGE);
  const el = document.getElementById('pagination');
  if (total <= 1) {{ el.innerHTML = ''; return; }}
  const block = Math.floor((curPage-1)/10);
  const s = block*10+1, e = Math.min(s+9, total);
  let h = '<button class="page-btn" onclick="goPage(1)" '+(curPage===1?'disabled':'')+'>&laquo;</button>';
  h += '<button class="page-btn" onclick="goPage('+(curPage-1)+')" '+(curPage===1?'disabled':'')+'>&#8249;</button>';
  for (let p=s; p<=e; p++) h += '<button class="page-btn '+(p===curPage?'active':'')+'" onclick="goPage('+p+')">' + p + '</button>';
  h += '<button class="page-btn" onclick="goPage('+(curPage+1)+')" '+(curPage===total?'disabled':'')+'>&#8250;</button>';
  h += '<button class="page-btn" onclick="goPage('+total+')" '+(curPage===total?'disabled':'')+'>&raquo;</button>';
  el.innerHTML = h;
}}

function goPage(p) {{
  const total = Math.ceil(filtered.length / PER_PAGE);
  if (p<1||p>total) return;
  curPage = p; openRow = null;
  render();
  window.scrollTo({{top:0,behavior:'smooth'}});
}}

// ── 새로고침 ──
let pollTimer = null;
function startRefresh() {{
  const btn = document.getElementById('btnRefresh');
  const icon = document.getElementById('refreshIcon');
  btn.disabled = true;
  icon.className = 'spinning';
  showToast('크롤링 시작...');
  fetch(API + '/refresh', {{method:'POST'}})
    .then(r=>r.json())
    .then(res => {{
      if (res.ok) pollTimer = setInterval(pollStatus, 2500);
      else {{ showToast('오류: '+(res.error||'알 수 없음')); resetBtn(); }}
    }})
    .catch(() => {{
      showToast('서버 미연결. 먼저 scourt_server.py를 실행해주세요.');
      resetBtn();
    }});
}}

function pollStatus() {{
  fetch(API + '/status').then(r=>r.json()).then(res => {{
    showToast(res.message);
    if (!res.running) {{
      clearInterval(pollTimer);
      fetch(API + '/data').then(r=>r.json()).then(newData => {{
        DATA = newData['공고목록'];
        document.getElementById('metaInfo').innerHTML =
          '수집: ' + newData['수집일시'] + '<br>총 <strong style="font-size:1.05rem">' + newData['총건수'].toLocaleString() + '</strong>건';
        buildTabs(); resetFilter();
        setTimeout(hideToast, 3000);
        resetBtn();
      }});
    }}
  }}).catch(() => {{ clearInterval(pollTimer); resetBtn(); }});
}}

function resetBtn() {{
  const btn = document.getElementById('btnRefresh');
  btn.disabled = false;
  document.getElementById('refreshIcon').className = '';
  document.getElementById('refreshIcon').innerHTML = '&#x21bb;';
}}

function showToast(msg) {{ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); }}
function hideToast() {{ document.getElementById('toast').classList.remove('show'); }}

buildTabs();
filtered = [...DATA];
render();
</script>
</body>
</html>"""


def main():
    json_path = Path(CONFIG["output_json"])
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        Path(CONFIG["output_html"]).write_text(HTML_뷰어_생성(data), encoding="utf-8")
        crawl_status["last_updated"] = data.get("수집일시", "")
        crawl_status["total"] = data.get("총건수", 0)
        logger.info(f"기존 데이터 로드: {data.get('총건수', 0)}건")

    port = CONFIG["port"]
    host = CONFIG["host"]
    server = HTTPServer((host, port), Handler)
    logger.info(f"서버 시작: http://localhost:{port}")
    logger.info(f"뷰어: http://localhost:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
