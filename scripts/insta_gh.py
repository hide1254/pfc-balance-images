"""GitHub Actions で動く Instagram 自動投稿（標準ライブラリだけで動く）。

    python scripts/insta_gh.py notify    # 前日18:00：翌日分の確認ページ（Issue）を作り、通知する
    python scripts/insta_gh.py remind    # 前日20:00／当日9:00：返事がなければ念押し
    python scripts/insta_gh.py post      # 当日19:00：「はい」の投稿だけを投稿
    python scripts/insta_gh.py refresh   # 毎週：インスタのトークンを延長して Secret を更新

必要な環境変数：IG_ACCESS_TOKEN, IG_USER_ID, GITHUB_TOKEN, GITHUB_REPOSITORY, OWNER（返事できる人）, GH_PAT（refresh のみ）
予定は schedule.json、状態は state.json（この保管庫の中）。
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = dt.timezone(dt.timedelta(hours=9))
REPO = os.environ.get("GITHUB_REPOSITORY", "hide1254/pfc-balance-images")
OWNER = os.environ.get("OWNER", "hide1254")
GRAPH = "https://graph.instagram.com/v23.0/"
WEEK = "月火水木金土日"
YES = ("はい", "ハイ", "yes", "ok", "ＯＫ", "了解")
NO = ("いいえ", "no", "やめる", "ストップ")


# ---------- 共通 ----------
def now():
    return dt.datetime.now(JST)


def load(name, default):
    p = ROOT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name, data):
    (ROOT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def entry_for(day: dt.date):
    for e in load("schedule.json", {"schedule": []})["schedule"]:
        if e["date"] == day.isoformat():
            return e
    return None


def post_data(e):
    return json.loads((ROOT / "posts" / e["id"] / "post.json").read_text(encoding="utf-8"))


def raw(path):
    return f"https://raw.githubusercontent.com/{REPO}/main/{path}"


def label(day: dt.date):
    return f"{day.month}/{day.day}（{WEEK[day.weekday()]}）"


def http(method, url, body=None, headers=None, form=False):
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url.split('?')[0]} → {e.code} {e.read().decode()[:300]}")


def gh(method, path, body=None, token=None):
    return http(method, "https://api.github.com/" + path, body, {
        "Authorization": "Bearer " + (token or os.environ["GITHUB_TOKEN"]),
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})


def ig(method, path, params):
    params = dict(params, access_token=os.environ["IG_ACCESS_TOKEN"])
    if method == "POST":
        return http("POST", GRAPH + path, params, form=True)
    return http("GET", GRAPH + path + "?" + urllib.parse.urlencode(params))


def comment(issue, text):
    gh("POST", f"repos/{REPO}/issues/{issue}/comments", {"body": text})


def decision(issue):
    """持ち主（OWNER）の最後の「はい」「いいえ」を返す。無ければ None。"""
    ans = None
    page = 1
    while True:
        cs = gh("GET", f"repos/{REPO}/issues/{issue}/comments?per_page=100&page={page}")
        for c in cs:
            if c["user"]["login"].lower() != OWNER.lower():
                continue
            t = c["body"].strip().lower()
            if any(t.startswith(w) for w in NO):
                ans = "いいえ"
            elif any(t.startswith(w.lower()) for w in YES):
                ans = "はい"
        if len(cs) < 100:
            break
        page += 1
    return ans


# ---------- 前日18:00：確認ページを作る ----------
def notify():
    day = now().date() + dt.timedelta(days=1)
    e = entry_for(day)
    if not e:
        print(f"{day} の予定なし")
        return
    state = load("state.json", {})
    st = state.get(e["id"], {})
    if st.get("issue"):
        print(f"{e['id']} の確認ページは作成済み #{st['issue']}")
        return
    p = post_data(e)
    v = now().strftime("%Y%m%d%H%M")
    imgs = "\n".join(f'<img src="{raw(f"posts/{e["id"]}/post_{n}.jpg")}?v={v}" width="320">' for n in range(1, p["slides"] + 1))
    body = (f"@{OWNER} 明日 **{label(day)} 19:00** に投稿する予定の内容です。\n\n"
            f"### 返事のしかた\n下のコメント欄に **「はい」** か **「いいえ」** とだけ書いて送ってください。\n"
            f"- 「はい」→ 明日19時ごろに投稿します\n- 「いいえ」→ 投稿しません（Claudeに「直して」と伝えてください）\n"
            f"- 返事がないまま明日19時になったら、投稿しません\n\n"
            f"### 5枚の画像\n{imgs}\n\n### キャプション\n```\n{p['caption']}\n\n{' '.join(p['hashtags'])}\n```\n")
    issue = gh("POST", f"repos/{REPO}/issues", {"title": f"【確認】{label(day)} {p['theme']}", "body": body,
                                                  "labels": ["確認待ち"]})
    st.update({"date": e["date"], "theme": p["theme"], "status": "確認待ち", "issue": issue["number"]})
    state[e["id"]] = st
    save("state.json", state)
    print(f"確認ページを作成 #{issue['number']}")


# ---------- 念押し ----------
def remind():
    t = now()
    day = t.date() + dt.timedelta(days=1) if t.hour >= 12 else t.date()
    e = entry_for(day)
    if not e:
        print(f"{day} の予定なし")
        return
    st = load("state.json", {}).get(e["id"], {})
    if not st.get("issue") or st.get("status") == "投稿済み":
        return
    if decision(st["issue"]):
        print("返事あり")
        return
    when = "明日" if day != t.date() else "今日"
    comment(st["issue"], f"@{OWNER} まだ返事がありません。{when} 19:00 に投稿してよければ「はい」、やめる場合は「いいえ」と書いてください。")
    print("念押しを送った")


# ---------- 当日19:00：投稿 ----------
def wait_container(cid, limit=300):
    start = time.time()
    while time.time() - start < limit:
        s = ig("GET", cid, {"fields": "status_code,status"})
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"インスタが画像を読み込めませんでした: {s}")
        time.sleep(5)
    raise TimeoutError("インスタの準備が時間内に終わりませんでした")


def post():
    # 18:50 に起動して、19:00 まで待つ（GitHub の遅れの分を見込む）
    target = now().replace(hour=19, minute=0, second=0, microsecond=0)
    if now() < target:
        time.sleep(min((target - now()).total_seconds(), 900))
    day = now().date()
    e = entry_for(day)
    if not e:
        print(f"{day} の予定なし")
        return
    state = load("state.json", {})
    st = state.get(e["id"], {})
    if st.get("status") == "投稿済み":
        print("投稿済み")
        return
    issue = st.get("issue")
    ans = decision(issue) if issue else None
    if ans != "はい":
        st["status"] = "保留" if ans == "いいえ" else "未回答"
        state[e["id"]] = st
        save("state.json", state)
        if issue:
            comment(issue, f"「はい」の返事がなかったため、今日は投稿しませんでした（{st['status']}）。")
            gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed", "labels": [st["status"]]})
        print(f"投稿しない（{st['status']}）")
        return
    p = post_data(e)
    ig_user = os.environ["IG_USER_ID"]
    children = []
    for n in range(1, p["slides"] + 1):
        c = ig("POST", f"{ig_user}/media", {"image_url": raw(f"posts/{e['id']}/post_{n}.jpg"), "is_carousel_item": "true"})
        wait_container(c["id"])
        children.append(c["id"])
    parent = ig("POST", f"{ig_user}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                              "caption": p["caption"] + "\n\n" + " ".join(p["hashtags"])})
    wait_container(parent["id"])
    media = ig("POST", f"{ig_user}/media_publish", {"creation_id": parent["id"]})
    link = ig("GET", media["id"], {"fields": "permalink"}).get("permalink", "")
    st.update({"status": "投稿済み", "media_id": media["id"], "permalink": link,
               "posted_at": now().isoformat(timespec="seconds")})
    state[e["id"]] = st
    save("state.json", state)
    if issue:
        comment(issue, f"投稿しました ✅ {link}")
        gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed", "labels": ["投稿済み"]})
    print("投稿完了", link)


# ---------- 毎週：トークン延長 ----------
def refresh():
    q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": os.environ["IG_ACCESS_TOKEN"]})
    d = http("GET", "https://graph.instagram.com/refresh_access_token?" + q)
    from nacl import encoding, public  # refresh のときだけ使う（ワークフローで pip install）
    pat = os.environ["GH_PAT"]
    key = gh("GET", f"repos/{REPO}/actions/secrets/public-key", token=pat)
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(d["access_token"].encode())).decode()
    gh("PUT", f"repos/{REPO}/actions/secrets/IG_ACCESS_TOKEN", {"encrypted_value": enc, "key_id": key["key_id"]}, token=pat)
    state = load("state.json", {})
    state["_token"] = {"refreshed": now().date().isoformat(),
                       "expires_about": (now().date() + dt.timedelta(seconds=d.get("expires_in", 5184000))).isoformat()}
    save("state.json", state)
    print("トークン延長：成功", state["_token"])


# ---------- テスト（投稿はしない） ----------
def test():
    """予定表の最初の投稿で【テスト】の確認ページを作る。予定や状態は変えない。"""
    e = load("schedule.json", {"schedule": []})["schedule"][0]
    p = post_data(e)
    imgs = "\n".join(f'<img src="{raw(f"posts/{e["id"]}/post_{n}.jpg")}" width="320">' for n in range(1, p["slides"] + 1))
    body = (f"@{OWNER} これは**テスト**です。投稿はされません。\n\n"
            f"携帯に通知が届いたか、画像とキャプションが見えるかを確かめてください。\n"
            f"確かめたら、下のコメント欄に **「はい」** と書いて送ってください。\n\n### 5枚の画像\n{imgs}\n\n"
            f"### キャプション\n```\n{p['caption']}\n\n{' '.join(p['hashtags'])}\n```\n")
    issue = gh("POST", f"repos/{REPO}/issues", {"title": f"【テスト】確認ページの練習（{p['theme']}）", "body": body})
    state = load("state.json", {})
    state["_test"] = {"issue": issue["number"]}
    save("state.json", state)
    print("テストの確認ページ #", issue["number"])


def testcheck():
    """テストの確認ページの返事を読んで、結果をコメントして閉じる。"""
    state = load("state.json", {})
    issue = state.get("_test", {}).get("issue")
    if not issue:
        print("テストなし")
        return
    ans = decision(issue)
    comment(issue, f"テストの結果：返事を「{ans or 'まだなし'}」と受け取りました。本番でも同じように読み取ります。")
    if ans:
        gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed"})
        state.pop("_test")
        save("state.json", state)
    print("テストの返事:", ans)


if __name__ == "__main__":
    {"notify": notify, "remind": remind, "post": post, "refresh": refresh,
     "test": test, "testcheck": testcheck}[sys.argv[1]]()
