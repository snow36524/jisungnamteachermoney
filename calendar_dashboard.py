import os
import json
import datetime
import secrets
import hashlib
import base64
import urllib.parse
import requests as req_lib
from flask import Flask, redirect, request, session, render_template_string
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

if not os.environ.get('REDIRECT_URI'):
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'calendar-dashboard-secret-key')

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/tasks',
]
CREDENTIALS_FILE = 'credentials.json'
REDIRECT_URI = os.environ.get('REDIRECT_URI', 'http://localhost:8080/callback')

HTML = '''
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>캘린더 대시보드</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f7; color: #1d1d1f; min-height: 100vh; }
  .header { background: white; border-bottom: 1px solid #e5e5e5; padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 10; }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header .date { font-size: 14px; color: #666; }
  .container { max-width: 800px; margin: 0 auto; padding: 24px 16px; }
  .stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 24px; }
  .stat { background: white; border-radius: 12px; padding: 16px; border: 1px solid #e5e5e5; }
  .stat-n { font-size: 28px; font-weight: 600; color: #1d1d1f; }
  .stat-l { font-size: 13px; color: #888; margin-top: 4px; }
  .section-title { font-size: 13px; font-weight: 500; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .section { margin-bottom: 24px; }
  .event-card { background: white; border-radius: 12px; border: 1px solid #e5e5e5; padding: 14px 16px; margin-bottom: 8px; display: flex; align-items: flex-start; gap: 14px; }
  .event-time { font-size: 13px; color: #888; min-width: 48px; padding-top: 2px; }
  .event-dot { width: 8px; height: 8px; border-radius: 50%; margin-top: 5px; flex-shrink: 0; }
  .event-title { font-size: 15px; font-weight: 500; }
  .event-sub { font-size: 13px; color: #888; margin-top: 3px; }
  .tag { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 6px; margin-left: 8px; background: #f0f0f5; color: #666; font-weight: 400; }
  .tag.urgent { background: #fff0f0; color: #c00; }
  .task-add { display: flex; gap: 8px; margin-bottom: 10px; }
  .task-add input { flex: 1; padding: 10px 14px; border: 1px solid #e5e5e5; border-radius: 10px; font-size: 14px; outline: none; background: white; }
  .task-add input:focus { border-color: #9C27B0; }
  .task-add button { padding: 10px 18px; background: #9C27B0; color: white; border: none; border-radius: 10px; font-size: 14px; cursor: pointer; }
  .task-add button:hover { background: #7B1FA2; }
  .task-check { width: 18px; height: 18px; cursor: pointer; accent-color: #9C27B0; flex-shrink: 0; margin-top: 3px; }
  .event-add { background: white; border: 1px solid #e5e5e5; border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; }
  .event-add-row { display: flex; gap: 8px; margin-bottom: 8px; }
  .event-add-row:last-child { margin-bottom: 0; }
  .event-add input { flex: 1; padding: 8px 12px; border: 1px solid #e5e5e5; border-radius: 8px; font-size: 14px; outline: none; background: #f9f9f9; }
  .event-add input:focus { border-color: #1a73e8; background: white; }
  .event-add button { padding: 8px 18px; background: #1a73e8; color: white; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; white-space: nowrap; }
  .event-add button:hover { background: #1557b0; }
  .empty { text-align: center; color: #aaa; font-size: 14px; padding: 32px; background: white; border-radius: 12px; border: 1px solid #e5e5e5; }
  .login-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .login-card { background: white; border-radius: 16px; padding: 40px; text-align: center; border: 1px solid #e5e5e5; max-width: 360px; width: 100%; }
  .login-card h2 { font-size: 22px; margin-bottom: 8px; }
  .login-card p { color: #888; font-size: 14px; margin-bottom: 24px; }
  .btn { display: inline-block; padding: 12px 24px; background: #1a73e8; color: white; border-radius: 8px; text-decoration: none; font-size: 15px; font-weight: 500; }
  .btn:hover { background: #1557b0; }
  .refresh { font-size: 13px; color: #1a73e8; text-decoration: none; }
  .refresh:hover { text-decoration: underline; }
  @media (max-width: 480px) { .stat-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
{% if not logged_in %}
<div class="login-wrap">
  <div class="login-card">
    <h2>📅 캘린더 대시보드</h2>
    <p>Google 계정으로 로그인하면<br>일정을 한눈에 볼 수 있어요</p>
    <a class="btn" href="/login">Google로 로그인</a>
  </div>
</div>
{% else %}
<div class="header">
  <h1>📅 캘린더 대시보드</h1>
  <div style="display:flex;align-items:center;gap:16px">
    <span class="date">{{ today }}</span>
    <a class="refresh" href="/">새로고침</a>
    <a class="refresh" href="/logout" style="color:#888">로그아웃</a>
  </div>
</div>
<div class="container">
  <div class="stat-grid">
    <div class="stat">
      <div class="stat-n">{{ today_count }}</div>
      <div class="stat-l">오늘 일정</div>
    </div>
    <div class="stat">
      <div class="stat-n">{{ week_count }}</div>
      <div class="stat-l">이번 주 일정</div>
    </div>
    <div class="stat">
      <div class="stat-n">{{ tomorrow_count }}</div>
      <div class="stat-l">내일 일정</div>
    </div>
    <div class="stat">
      <div class="stat-n">{{ tasks_count }}</div>
      <div class="stat-l">미완료 할일</div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">일정 추가</div>
    <form class="event-add" action="/event/add" method="post">
      <div class="event-add-row">
        <input type="text" name="title" placeholder="일정 제목" required>
      </div>
      <div class="event-add-row">
        <input type="date" name="date" required value="{{ today_date }}">
        <input type="time" name="start_time" value="09:00">
        <input type="time" name="end_time" value="10:00">
        <button type="submit">추가</button>
      </div>
    </form>
  </div>

  <div class="section">
    <div class="section-title">오늘 · {{ today }}</div>
    {% if today_events %}
      {% for e in today_events %}
      <div class="event-card">
        <div class="event-time">{{ e.time }}</div>
        <div class="event-dot" style="background:{{ e.color }}"></div>
        <div>
          <div class="event-title">{{ e.title }}{% if e.allday %}<span class="tag">종일</span>{% endif %}</div>
          {% if e.location %}<div class="event-sub">📍 {{ e.location }}</div>{% endif %}
          {% if e.duration %}<div class="event-sub">{{ e.duration }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">오늘 일정이 없어요</div>
    {% endif %}
  </div>

  <div class="section">
    <div class="section-title">내일</div>
    {% if tomorrow_events %}
      {% for e in tomorrow_events %}
      <div class="event-card">
        <div class="event-time">{{ e.time }}</div>
        <div class="event-dot" style="background:{{ e.color }}"></div>
        <div>
          <div class="event-title">{{ e.title }}{% if e.allday %}<span class="tag">종일</span>{% endif %}</div>
          {% if e.location %}<div class="event-sub">📍 {{ e.location }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">내일 일정이 없어요</div>
    {% endif %}
  </div>

  <div class="section">
    <div class="section-title">이번 주 남은 일정</div>
    {% if week_events %}
      {% for e in week_events %}
      <div class="event-card">
        <div class="event-time">{{ e.day }}<br>{{ e.time }}</div>
        <div class="event-dot" style="background:{{ e.color }}"></div>
        <div>
          <div class="event-title">{{ e.title }}</div>
          {% if e.location %}<div class="event-sub">📍 {{ e.location }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">이번 주 남은 일정이 없어요</div>
    {% endif %}
  </div>

  <div class="section">
    <div class="section-title">할일 (Google Tasks)</div>
    <form class="task-add" action="/task/add" method="post">
      <input type="text" name="title" placeholder="할일 추가..." required>
      <button type="submit">추가</button>
    </form>
    {% if tasks %}
      {% for t in tasks %}
      <div class="event-card" id="task-{{ t.id }}">
        <form action="/task/complete" method="post" style="margin-top:3px">
          <input type="hidden" name="task_id" value="{{ t.id }}">
          <input type="hidden" name="tasklist_id" value="{{ t.tasklist_id }}">
          <input class="task-check" type="checkbox" onchange="this.form.submit()" title="완료로 표시">
        </form>
        <div class="event-dot" style="background:#9C27B0"></div>
        <div style="flex:1">
          <div class="event-title">{{ t.title }}</div>
          {% if t.due %}<div class="event-sub">마감: {{ t.due }}</div>{% endif %}
          {% if t.notes %}<div class="event-sub">{{ t.notes }}</div>{% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">미완료 할일이 없어요</div>
    {% endif %}
  </div>
</div>
{% endif %}
</body>
</html>
'''

COLORS = ['#4285F4', '#0F9D58', '#F4B400', '#DB4437', '#AB47BC', '#00ACC1', '#FF7043', '#9E9D24']

def load_client_config():
    with open(CREDENTIALS_FILE) as f:
        data = json.load(f)
    info = data.get('web') or data.get('installed')
    return info

def make_pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return verifier, challenge

def get_service():
    if 'credentials' not in session:
        return None
    c = session['credentials']
    if not c.get('token'):
        return None
    try:
        creds = Credentials(
            token=c.get('token'),
            refresh_token=c.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=c.get('client_id'),
            client_secret=c.get('client_secret'),
            scopes=c.get('scopes'),
        )
        return build('calendar', 'v3', credentials=creds)
    except Exception:
        session.clear()
        return None

def format_event(e, show_day=False):
    start = e.get('start', {})
    end = e.get('end', {})
    allday = 'date' in start and 'dateTime' not in start

    if allday:
        time_str = '종일'
    else:
        dt = datetime.datetime.fromisoformat(start['dateTime'])
        time_str = dt.strftime('%H:%M')

    duration = ''
    if not allday and 'dateTime' in end:
        s = datetime.datetime.fromisoformat(start['dateTime'])
        en = datetime.datetime.fromisoformat(end['dateTime'])
        mins = int((en - s).total_seconds() / 60)
        if mins >= 60:
            h, m = divmod(mins, 60)
            duration = f'{h}시간' + (f' {m}분' if m else '')
        else:
            duration = f'{mins}분'

    color_idx = hash(e.get('id', '')) % len(COLORS)
    day_str = ''
    if show_day and not allday:
        dt = datetime.datetime.fromisoformat(start['dateTime'])
        days = ['월', '화', '수', '목', '금', '토', '일']
        day_str = f"{dt.month}/{dt.day}({days[dt.weekday()]})"

    return {
        'title': e.get('summary', '(제목 없음)'),
        'time': time_str,
        'color': COLORS[color_idx],
        'location': e.get('location', ''),
        'duration': duration,
        'allday': allday,
        'day': day_str,
    }

@app.route('/')
def index():
    try:
        service = get_service()
    except Exception:
        session.clear()
        service = None
    if not service:
        return render_template_string(HTML, logged_in=False)

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    week_end = today + datetime.timedelta(days=7)

    def dt_to_rfc(d):
        return datetime.datetime.combine(d, datetime.time.min).replace(
            tzinfo=datetime.timezone(datetime.timedelta(hours=9))).isoformat()

    def fetch(time_min, time_max):
        result = service.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime',
            maxResults=50
        ).execute()
        return result.get('items', [])

    today_evts = fetch(dt_to_rfc(today), dt_to_rfc(tomorrow))
    tomorrow_evts = fetch(dt_to_rfc(tomorrow), dt_to_rfc(tomorrow + datetime.timedelta(days=1)))
    week_evts = fetch(dt_to_rfc(tomorrow + datetime.timedelta(days=1)), dt_to_rfc(week_end))

    # Tasks 가져오기
    tasks_list = []
    try:
        c = session['credentials']
        creds = Credentials(
            token=c.get('token'),
            refresh_token=c.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=c.get('client_id'),
            client_secret=c.get('client_secret'),
            scopes=c.get('scopes'),
        )
        tasks_service = build('tasks', 'v1', credentials=creds)
        task_lists = tasks_service.tasklists().list().execute().get('items', [])
        for tl in task_lists:
            items = tasks_service.tasks().list(
                tasklist=tl['id'],
                showCompleted=False,
                maxResults=20
            ).execute().get('items', [])
            for t in items:
                if t.get('status') != 'completed':
                    due = ''
                    if t.get('due'):
                        d = datetime.datetime.fromisoformat(t['due'].replace('Z', '+00:00'))
                        due = f"{d.month}월 {d.day}일"
                    tasks_list.append({
                        'id': t.get('id', ''),
                        'tasklist_id': tl['id'],
                        'title': t.get('title', ''),
                        'due': due,
                        'notes': t.get('notes', ''),
                    })
    except Exception:
        pass

    days = ['월', '화', '수', '목', '금', '토', '일']
    today_str = f"{today.month}월 {today.day}일 ({days[today.weekday()]})"

    return render_template_string(HTML,
        today_date=today.isoformat(),
        logged_in=True,
        today=today_str,
        today_events=[format_event(e) for e in today_evts],
        tomorrow_events=[format_event(e) for e in tomorrow_evts],
        week_events=[format_event(e, show_day=True) for e in week_evts],
        today_count=len(today_evts),
        tomorrow_count=len(tomorrow_evts),
        week_count=len(today_evts) + len(tomorrow_evts) + len(week_evts),
        tasks=tasks_list,
        tasks_count=len(tasks_list),
    )

@app.route('/login')
def login():
    verifier, challenge = make_pkce()
    session['pkce_verifier'] = verifier
    state = secrets.token_urlsafe(16)
    session['state'] = state
    cfg = load_client_config()
    params = {
        'client_id': cfg['client_id'],
        'redirect_uri': REDIRECT_URI,
        'response_type': 'code',
        'scope': ' '.join(SCOPES),
        'state': state,
        'access_type': 'offline',
        'prompt': 'consent',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode(params)
    return redirect(auth_url)

@app.route('/callback')
def callback():
    code = request.args.get('code')
    cfg = load_client_config()
    resp = req_lib.post('https://oauth2.googleapis.com/token', data={
        'code': code,
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'redirect_uri': REDIRECT_URI,
        'grant_type': 'authorization_code',
        'code_verifier': session.get('pkce_verifier'),
    })
    token_data = resp.json()
    session['credentials'] = {
        'token': token_data.get('access_token'),
        'refresh_token': token_data.get('refresh_token'),
        'token_uri': 'https://oauth2.googleapis.com/token',
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'scopes': SCOPES,
    }
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

def get_tasks_service():
    c = session['credentials']
    creds = Credentials(
        token=c.get('token'),
        refresh_token=c.get('refresh_token'),
        token_uri='https://oauth2.googleapis.com/token',
        client_id=c.get('client_id'),
        client_secret=c.get('client_secret'),
        scopes=c.get('scopes'),
    )
    return build('tasks', 'v1', credentials=creds)

@app.route('/event/add', methods=['POST'])
def event_add():
    if 'credentials' not in session:
        return redirect('/')
    title = request.form.get('title', '').strip()
    date = request.form.get('date')
    start_time = request.form.get('start_time', '09:00')
    end_time = request.form.get('end_time', '10:00')
    if not title or not date:
        return redirect('/')
    try:
        c = session['credentials']
        creds = Credentials(
            token=c.get('token'),
            refresh_token=c.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=c.get('client_id'),
            client_secret=c.get('client_secret'),
            scopes=c.get('scopes'),
        )
        svc = build('calendar', 'v3', credentials=creds)
        event = {
            'summary': title,
            'start': {'dateTime': f'{date}T{start_time}:00', 'timeZone': 'Asia/Seoul'},
            'end':   {'dateTime': f'{date}T{end_time}:00',   'timeZone': 'Asia/Seoul'},
        }
        svc.events().insert(calendarId='primary', body=event).execute()
    except Exception:
        pass
    return redirect('/')



@app.route('/task/add', methods=['POST'])
def task_add():
    title = request.form.get('title', '').strip()
    if not title or 'credentials' not in session:
        return redirect('/')
    try:
        svc = get_tasks_service()
        tasklists = svc.tasklists().list().execute().get('items', [])
        tasklist_id = tasklists[0]['id'] if tasklists else '@default'
        svc.tasks().insert(tasklist=tasklist_id, body={'title': title}).execute()
    except Exception:
        pass
    return redirect('/')

@app.route('/task/complete', methods=['POST'])
def task_complete():
    task_id = request.form.get('task_id')
    tasklist_id = request.form.get('tasklist_id')
    if not task_id or 'credentials' not in session:
        return redirect('/')
    try:
        svc = get_tasks_service()
        svc.tasks().patch(
            tasklist=tasklist_id,
            task=task_id,
            body={'status': 'completed'}
        ).execute()
    except Exception:
        pass
    return redirect('/')

if __name__ == '__main__':
    app.run(port=8080, debug=False)
