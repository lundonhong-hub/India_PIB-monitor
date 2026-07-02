#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PIB Monitor — 인도 정부 보도자료(PIB) RSS 감시기

인도 Press Information Bureau의 National 보도자료 RSS를 주기적으로 확인해서,
지정 키워드(copper, QCO, anti-dumping 등)가 제목에 걸린 신규 발표가 뜨면
Gmail로 알림 메일을 보낸다.

- egazette monitor의 "3차 그물(확정 관보)"에 대응하는 "1차 그물(빠른 발표)".
- 브라우저(Playwright) 불필요. requests로 RSS(XML)만 받으면 끝 → 가볍고 안정적.
- PRID(보도자료 고유번호)로 중복 관리. (PIB RSS엔 pubDate가 없다)
- GitHub Actions에서 무료 자동 실행.

설정은 아래 CONFIG 블록만 만지면 된다.
"""

import os
import sys
import json
import time
import smtplib
import urllib.request
import ssl
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate

# ============================================================
# CONFIG — 여기만 만지면 됨
# ============================================================

# PIB National 보도자료 RSS.
# ★중요: 아래 Regid 값은 사이트에서 Region을 "National"로 고른 뒤
#   Press Releases RSS 링크의 실제 URL을 확인해서 그 값으로 맞출 것.
#   (화면에서 본 목록은 지역=Mumbai 기준이었음. National은 Regid가 다를 수 있음.)
RSS_URL = "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"

# 감시 키워드 (제목에서 소문자로 매칭). PIB RSS엔 본문(description)이 없어서
# 제목에만 걸린다 → 넓게 잡아두고 메일 받아보며 조절하는 게 실전.
KEYWORDS = [
    # --- 티어1: 제품 직결 ---
    "copper", "minister",
    "copper tube",
    "copper pipe",
    "copper alloy",
    "refined copper",
    "hindustan copper",    
    # --- 티어2: 무역구제 (수출기업 최우선) ---
    "anti-dumping",
    "anti dumping",
    "antidumping",
    "countervailing",
    "safeguard duty",
    "dgtr",
    "trade remedy",
    "dumping duty",
    # --- 티어3: 품질규제/BIS ---
    "bis",
    "quality control order",
    "qco",
    "is 10773",
    "bis certification",
    "mandatory certification",
    # --- 티어4: 관세/무역정책 ---
    "customs duty",
    "import duty",
    "basic customs duty",
    "bcd",
    "import policy",
    "tariff",
    "hs code",
    "import restriction",
    # --- 티어5: 국가/협정 (베트남 공장 핵심) ---
    "asean",
    "asean-india",
    "vietnam",
    "free trade agreement",
    "fta",
    "rules of origin",
    "ceca",
    "korea",
    "india-korea",
]

STATE_FILE = "state.json"          # 이미 알린 PRID 목록
MAX_STATE = 5000                   # state.json 무한증식 방지 (오래된 것부터 버림)
REQUEST_TIMEOUT = 30

# Gmail (값은 GitHub Secrets에서 환경변수로 주입)
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
ALERT_TO = os.environ.get("ALERT_TO", "")   # 콤마로 다중 수신 가능

# 브라우저처럼 보이는 헤더 (PIB가 기본 UA를 403으로 막음)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ============================================================
# 이하 로직
# ============================================================


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch_rss(url, retries=3):
    """RSS XML을 받아서 문자열로 반환. 실패 시 재시도."""
    ctx = ssl.create_default_context()
    # PIB(NIC 호스팅)는 인증서 체인이 불안정할 때가 있어 검증 완화.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(req, context=ctx, timeout=REQUEST_TIMEOUT) as r:
                data = r.read().decode("utf-8", errors="replace")
                if "<item>" not in data and "<item >" not in data:
                    raise ValueError("응답에 <item>이 없음 (빈 피드/차단 의심)")
                log(f"RSS 수신 성공 (len={len(data)}, 시도 {attempt})")
                return data
        except Exception as e:
            last_err = e
            log(f"RSS 수신 실패 (시도 {attempt}/{retries}): {e!r}")
            time.sleep(3 * attempt)
    raise RuntimeError(f"RSS 수신 최종 실패: {last_err!r}")


def parse_items(xml_text):
    """XML에서 (prid, title, link) 목록 추출."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        # 가끔 앞뒤로 <script/> 같은 잡음이 섞여 파싱 실패 → channel부터 잘라 재시도
        log(f"XML 파싱 오류, 보정 시도: {e}")
        start = xml_text.find("<channel>")
        end = xml_text.rfind("</channel>")
        if start != -1 and end != -1:
            patched = "<rss><channel>" + xml_text[start + len("<channel>"):end] + "</channel></rss>"
            root = ET.fromstring(patched)
        else:
            raise

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        prid = extract_prid(link)
        if prid:
            items.append({"prid": prid, "title": title, "link": link})
    log(f"파싱된 item 수: {len(items)}")
    return items


def extract_prid(link):
    """링크에서 PRID= 뒤 숫자 추출."""
    key = "PRID="
    idx = link.find(key)
    if idx == -1:
        return ""
    tail = link[idx + len(key):]
    num = ""
    for ch in tail:
        if ch.isdigit():
            num += ch
        else:
            break
    return num


def match_keywords(title):
    """제목에 키워드가 걸리면 걸린 키워드 리스트 반환."""
    low = title.lower()
    hits = [kw for kw in KEYWORDS if kw.lower() in low]
    return hits


def load_state():
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        log(f"state.json 로드 실패 (빈 목록으로 시작): {e}")
        return []


def save_state(prids):
    # 최신 MAX_STATE개만 유지
    trimmed = prids[-MAX_STATE:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=0)
    log(f"state.json 저장 (총 {len(trimmed)}개 PRID 기억)")


def send_email(matches):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and ALERT_TO):
        log("메일 설정(GMAIL_USER/GMAIL_APP_PASSWORD/ALERT_TO) 없음 → 발송 생략")
        return

    to_list = [x.strip() for x in ALERT_TO.split(",") if x.strip()]

    subject = f"[PIB 알림] 신규 {len(matches)}건 — {matches[0]['title'][:40]}"
    if len(matches) > 1:
        subject += f" 외 {len(matches) - 1}건"

    lines = []
    lines.append(f"<h2>PIB 신규 보도자료 {len(matches)}건 감지</h2>")
    lines.append("<p>인도 Press Information Bureau(National) RSS에서 키워드가 걸린 발표입니다.</p>")
    lines.append("<hr>")
    for m in matches:
        kw = ", ".join(m["hits"])
        lines.append(
            f"<p><b>{escape_html(m['title'])}</b><br>"
            f"걸린 키워드: <span style='color:#c00'>{escape_html(kw)}</span><br>"
            f"PRID: {m['prid']}<br>"
            f"<a href='{escape_html(m['link'])}'>{escape_html(m['link'])}</a></p>"
        )
    lines.append("<hr>")
    lines.append("<p style='color:#888;font-size:12px'>"
                 "이 메일은 PIB Monitor(1차 그물)가 자동 발송했습니다. "
                 "제목 기반 매칭이라 본문에만 키워드가 있는 건 놓칠 수 있습니다.</p>")
    html = "\n".join(lines)

    msg = MIMEMultipart("alternative")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=REQUEST_TIMEOUT) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to_list, msg.as_string())
    log(f"메일 발송 완료 → {to_list}")


def escape_html(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def main():
    log("=== PIB Monitor 시작 ===")

    seen = load_state()
    seen_set = set(seen)

    xml_text = fetch_rss(RSS_URL)
    items = parse_items(xml_text)

    matches = []
    new_prids = []
    for it in items:
        if it["prid"] in seen_set:
            continue
        new_prids.append(it["prid"])          # 신규는 봤으니 기억 (매칭 여부 무관)
        hits = match_keywords(it["title"])
        if hits:
            it["hits"] = hits
            matches.append(it)
            log(f"매칭: [{','.join(hits)}] {it['title'][:60]}")

    if matches:
        try:
            send_email(matches)
        except Exception as e:
            # 메일 실패 시 이번 신규를 기억에 넣지 않아 다음 회차에 재시도되게 함
            log(f"메일 발송 실패 → state 저장 생략(다음 회차 재시도): {e!r}")
            sys.exit(1)
    else:
        log("매칭 신규 없음")

    # 기억 갱신: 기존 + 이번에 본 신규 전부 (매칭 안 된 것도 다시 안 보게)
    save_state(seen + new_prids)
    log("=== PIB Monitor 종료 ===")


if __name__ == "__main__":
    main()
