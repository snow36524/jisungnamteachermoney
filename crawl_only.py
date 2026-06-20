"""GitHub Actions용 독립 크롤링 스크립트"""
import asyncio
import json
import random
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

BASE_URL = "https://www.scourt.go.kr/portal/notice/realestate/RealNoticeList.work"
DETAIL_BASE = "https://www.scourt.go.kr/portal/notice/realestate/RealNoticeView.work"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.scourt.go.kr/",
}


def 상세페이지_파싱(seq_id):
    url = f"{DETAIL_BASE}?seq_id={seq_id}"
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        try:
            html = raw.decode("euc-kr")
        except Exception:
            html = raw.decode("utf-8", errors="replace")
        files = []
        for m in re.finditer(
            r"javascript:download\(['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\)",
            html, re.IGNORECASE
        ):
            server_name = m.group(1)
            display_name = m.group(2)
            ext = server_name.rsplit(".", 1)[-1].lower() if "." in server_name else ""
            files.append({"name": display_name, "server_name": server_name, "ext": ext})
        return files
    except Exception as e:
        print(f"  상세페이지 오류 ({seq_id}): {e}")
        return []


async def 페이지_파싱(page):
    rows = await page.query_selector_all("table tbody tr")
    items = []
    for row in rows:
        try:
            cells = await row.query_selector_all("td")
            if len(cells) < 6:
                continue
            번호 = (await cells[0].inner_text()).strip()
            법원 = (await cells[1].inner_text()).strip()
            공고내용 = (await cells[2].inner_text()).strip()
            채무자 = (await cells[3].inner_text()).strip()
            종별_el = await cells[4].query_selector("span,a,td")
            종별 = (await cells[4].inner_text()).strip()
            조회 = (await cells[5].inner_text()).strip()

            link_el = await cells[2].query_selector("a")
            seq_id = None
            if link_el:
                href = await link_el.get_attribute("href") or ""
                onclick = await link_el.get_attribute("onclick") or ""
                m = re.search(r"seq_id=(\d+)", href + onclick)
                if m:
                    seq_id = m.group(1)

            if 번호 and 법원:
                items.append({
                    "번호": 번호, "법원": 법원, "공고내용": 공고내용,
                    "채무자": 채무자, "종별": 종별, "조회": 조회,
                    "seq_id": seq_id,
                })
        except Exception:
            continue
    return items


async def 총페이지수_확인(page):
    try:
        pager = await page.query_selector(".paging, .pagination, #paging")
        if pager:
            text = await pager.inner_text()
            nums = re.findall(r"\d+", text)
            if nums:
                return max(int(n) for n in nums)
        last_btn = await page.query_selector("a.last, .paging a:last-child")
        if last_btn:
            onclick = await last_btn.get_attribute("onclick") or ""
            href = await last_btn.get_attribute("href") or ""
            m = re.search(r"pageIndex=(\d+)", onclick + href)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 1


async def main():
    모든_공고 = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=HEADERS["User-Agent"])
        page = await context.new_page()
        page.set_default_timeout(30000)

        print("1페이지 접속 중...")
        await page.goto(BASE_URL)
        await page.wait_for_load_state("networkidle")
        총페이지 = await 총페이지수_확인(page)
        print(f"총 {총페이지}페이지")

        items = await 페이지_파싱(page)
        모든_공고.extend(items)

        for page_num in range(2, 총페이지 + 1):
            await asyncio.sleep(random.uniform(0.8, 1.5))
            print(f"{page_num}/{총페이지} 페이지 수집 중... ({len(모든_공고)}건)")
            try:
                await page.goto(f"{BASE_URL}?pageIndex={page_num}")
                await page.wait_for_load_state("networkidle")
                모든_공고.extend(await 페이지_파싱(page))
            except Exception as e:
                print(f"{page_num}페이지 오류: {e}")

        await browser.close()

    print(f"\n첨부파일 정보 수집 중... (총 {len(모든_공고)}건)")
    for i, item in enumerate(모든_공고):
        seq_id = item.get("seq_id")
        if seq_id:
            item["files"] = 상세페이지_파싱(seq_id)
        else:
            item["files"] = []
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(모든_공고)} 완료")
        time.sleep(0.4)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = {"수집일시": now, "총건수": len(모든_공고), "공고목록": 모든_공고}
    Path("scourt_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n완료! {len(모든_공고)}건 저장됨 ({now})")

    # 뷰어 HTML 재생성
    try:
        import scourt_server
        Path("scourt_viewer.html").write_text(
            scourt_server.HTML_뷰어_생성(data), encoding="utf-8"
        )
        print("scourt_viewer.html 재생성 완료")
    except Exception as e:
        print(f"HTML 재생성 오류: {e}")


if __name__ == "__main__":
    asyncio.run(main())
