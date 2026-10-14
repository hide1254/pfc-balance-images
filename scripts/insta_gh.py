"""GitHub Actions で動く Instagram 自動投稿（標準ライブラリだけで動く）。

    python scripts/insta_gh.py tick      # 10分ごと：今の時刻を見て、やるべきことだけを行う（ふだんはこれだけ）
    python scripts/insta_gh.py notify    # 翌日分の確認ページ（Issue）を作り、通知する
    python scripts/insta_gh.py post      # 今日の「はい」の投稿を投稿する
    python scripts/insta_gh.py refresh   # インスタのトークンを延長して Secret を更新
    python scripts/insta_gh.py test / testcheck   # 確認ページの練習（投稿はしない）

tick がすること（日本時間）
    18:00 以降        翌日分の確認ページを作る（作成済みなら何もしない）
    0:00〜19:00       今日分の確認ページが無ければ作る（前日に作れなかった時の予備）
    20:00 以降        翌日分に返事が無ければ念押し（1回だけ）
    9:00〜19:00       今日分に返事が無ければ念押し（1回だけ）
    19:00 以降        今日分を「はい」なら投稿、そうでなければ見送り（1回だけ）
    月曜 10:00 以降   トークン延長（週1回だけ）

必要な環境変数：IG_ACCESS_TOKEN, IG_USER_ID, GITHUB_TOKEN, GITHUB_REPOSITORY, OWNER（返事できる人）, GH_PAT（refresh のみ）
予定は schedule.json、状態は state.json（この保管庫の中）。state.json は API で「読んで・1か所だけ直して・書く」ので、
同時に動いても上書きし合わない。
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
        err = RuntimeError(f"{method} {url.split('?')[0]} → {e.code} {e.read().decode()[:300]}")
        err.code = e.code
        raise err


def gh(method, path, body=None, token=None):
    return http(method, "https://api.github.com/" + path, body, {
        "Authorization": "Bearer " + (token or os.environ["GITHUB_TOKEN"]),
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})


def ig(method, path, params):
    params = dict(params, access_token=os.environ["IG_ACCESS_TOKEN"])
    if method == "POST":
        return http("POST", GRAPH + path, params, form=True)
    return http("GET", GRAPH + path + "?" + urllib.parse.urlencode(params))


def read_json(name, default):
    p = ROOT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def state_get():
    """最新の state.json と、その版（sha）を GitHub から読む。"""
    try:
        c = gh("GET", f"repos/{REPO}/contents/state.json?ref=main")
    except RuntimeError as e:
        if getattr(e, "code", 0) == 404:
            return {}, None
        raise
    return json.loads(base64.b64decode(c["content"]).decode() or "{}"), c["sha"]


def state_update(key, changes, why):
    """state.json の key だけを直して保存する。ほかの実行が先に書いていたら、読み直してやり直す。"""
    for _ in range(6):
        st, sha = state_get()
        cur = st.get(key, {})
        cur.update(changes)
        st[key] = cur
        body = {"message": f"状態を更新：{why}", "branch": "main",
                "content": base64.b64encode((json.dumps(st, ensure_ascii=False, indent=2) + "\n").encode()).decode()}
        if sha:
            body["sha"] = sha
        try:
            gh("PUT", f"repos/{REPO}/contents/state.json", body)
            return cur
        except RuntimeError as e:
            if getattr(e, "code", 0) in (409, 422):
                time.sleep(2)
                continue
            raise
    raise RuntimeError("state.json の保存が混み合って失敗しました")


def entry_for(day: dt.date):
    for e in read_json("schedule.json", {"schedule": []})["schedule"]:
        if e["date"] == day.isoformat():
            return e
    return None


def post_data(e):
    return json.loads((ROOT / "posts" / e["id"] / "post.json").read_text(encoding="utf-8"))


def raw(path):
    return f"https://raw.githubusercontent.com/{REPO}/main/{path}"


def label(day: dt.date):
    return f"{day.month}/{day.day}（{WEEK[day.weekday()]}）"


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


# ---------- 確認ページを作る ----------
def notify(day=None):
    day = day or now().date() + dt.timedelta(days=1)
    e = entry_for(day)
    if not e:
        print(f"{day} の予定なし")
        return
    st, _ = state_get()
    s = st.get(e["id"], {})
    if s.get("issue") or s.get("status") in ("承認済み", "投稿済み"):
        return
    p = post_data(e)
    v = now().strftime("%Y%m%d%H%M")
    when = "明日" if day != now().date() else "今日"
    imgs = "\n".join(f'<img src="{raw(f"posts/{e["id"]}/post_{n}.jpg")}?v={v}" width="320">' for n in range(1, p["slides"] + 1))
    body = (f"@{OWNER} {when} **{label(day)} 19:00** に投稿する予定の内容です。\n\n"
            f"### 返事のしかた\n下のコメント欄に **「はい」** か **「いいえ」** とだけ書いて送ってください。\n"
            f"- 「はい」→ {when}19時ごろに投稿します\n- 「いいえ」→ 投稿しません（Claudeに「直して」と伝えてください）\n"
            f"- 返事がないまま{when}19時になったら、投稿しません\n\n"
            f"### 5枚の画像\n{imgs}\n\n### キャプション\n```\n{p['caption']}\n\n{' '.join(p['hashtags'])}\n```\n")
    issue = gh("POST", f"repos/{REPO}/issues", {"title": f"【確認】{label(day)} {p['theme']}", "body": body,
                                                  "labels": ["確認待ち"]})
    state_update(e["id"], {"date": e["date"], "theme": p["theme"], "status": "確認待ち", "issue": issue["number"]},
                 f"{e['id']} 確認ページ #{issue['number']}")
    print(f"確認ページを作成 #{issue['number']}")


# ---------- 念押し（1回だけ） ----------
def remind(day, flag):
    e = entry_for(day)
    if not e:
        return
    st, _ = state_get()
    s = st.get(e["id"], {})
    if not s.get("issue") or s.get("status") != "確認待ち" or s.get(flag):
        return
    if decision(s["issue"]):
        return
    when = "明日" if day != now().date() else "今日"
    comment(s["issue"], f"@{OWNER} まだ返事がありません。{when} 19:00 に投稿してよければ「はい」、やめる場合は「いいえ」と書いてください。")
    state_update(e["id"], {flag: now().isoformat(timespec="minutes")}, f"{e['id']} 念押し")
    print("念押しを送った")


# ---------- 投稿（1回だけ） ----------
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


def post(day=None):
    day = day or now().date()
    e = entry_for(day)
    if not e:
        print(f"{day} の予定なし")
        return
    st, _ = state_get()
    s = st.get(e["id"], {})
    if s.get("status") == "投稿済み" or s.get("closed"):
        return
    if s.get("posting"):
        started = dt.datetime.fromisoformat(s["posting"])
        if now() - started < dt.timedelta(minutes=20):
            print("ほかの実行が投稿中")
            return
    issue = s.get("issue")
    ans = decision(issue) if issue else None
    if not issue and s.get("status") == "承認済み":
        ans = "はい"  # Mac の仕組みで、すでに「はい」をもらっていた分
    if ans != "はい":
        status = "保留" if ans == "いいえ" else "未回答"
        state_update(e["id"], {"status": status, "closed": True}, f"{e['id']} 見送り")
        if issue:
            comment(issue, f"「はい」の返事がなかったため、今日は投稿しませんでした（{status}）。")
            gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed", "labels": [status]})
        print(f"投稿しない（{status}）")
        return
    # 投稿中のしるし（同時に別の実行が投稿しないように）
    state_update(e["id"], {"posting": now().isoformat(timespec="seconds")}, f"{e['id']} 投稿開始")
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
    state_update(e["id"], {"status": "投稿済み", "media_id": media["id"], "permalink": link,
                           "posted_at": now().isoformat(timespec="seconds")}, f"{e['id']} 投稿済み")
    if issue:
        comment(issue, f"投稿しました ✅ {link}")
        gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed", "labels": ["投稿済み"]})
    print("投稿完了", link)


# ---------- トークン延長 ----------
def refresh():
    q = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": os.environ["IG_ACCESS_TOKEN"]})
    d = http("GET", "https://graph.instagram.com/refresh_access_token?" + q)
    from nacl import encoding, public  # refresh のときだけ使う（ワークフローで pip install）
    pat = os.environ["GH_PAT"]
    key = gh("GET", f"repos/{REPO}/actions/secrets/public-key", token=pat)
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    enc = base64.b64encode(box.encrypt(d["access_token"].encode())).decode()
    gh("PUT", f"repos/{REPO}/actions/secrets/IG_ACCESS_TOKEN", {"encrypted_value": enc, "key_id": key["key_id"]}, token=pat)
    state_update("_token", {"refreshed": now().date().isoformat(),
                            "expires_about": (now().date() + dt.timedelta(seconds=d.get("expires_in", 5184000))).isoformat()},
                 "トークン延長")
    print("トークン延長：成功")


# ---------- 返事を状態に反映（表示用） ----------
def sync_decision(day):
    """返事が来ていれば、投稿を待たずに状態を「承認済み」「保留」に変える（投稿ノートの表示を正しくするため）。"""
    e = entry_for(day)
    if not e:
        return
    st, _ = state_get()
    s = st.get(e["id"], {})
    if not s.get("issue") or s.get("status") in ("投稿済み",) or s.get("closed"):
        return
    ans = decision(s["issue"])
    new = {"はい": "承認済み", "いいえ": "保留"}.get(ans, "確認待ち")
    if new != s.get("status"):
        state_update(e["id"], {"status": new}, f"{e['id']} 返事：{ans or 'なし'}")
        gh("PATCH", f"repos/{REPO}/issues/{s['issue']}", {"labels": [new]})
        print(f"{e['id']} の状態 → {new}")


# ---------- 10分ごと：今やるべきことだけ ----------
def tick():
    t = now()
    today, tomorrow = t.date(), t.date() + dt.timedelta(days=1)
    jobs = [("今日分の返事の反映", lambda: sync_decision(today)),
            ("翌日分の返事の反映", lambda: sync_decision(tomorrow))]
    if t.hour < 19:
        jobs.append(("今日分の確認ページ（予備）", lambda: notify(today)))
    if t.hour >= 18:
        jobs.append(("翌日分の確認ページ", lambda: notify(tomorrow)))
    if t.hour >= 20:
        jobs.append(("翌日分の念押し", lambda: remind(tomorrow, "reminded_eve")))
    if 9 <= t.hour < 19:
        jobs.append(("今日分の念押し", lambda: remind(today, "reminded_am")))
    if t.hour >= 19:
        jobs.append(("今日分の投稿", lambda: post(today)))
    if t.weekday() == 0 and t.hour >= 10:
        st, _ = state_get()
        if st.get("_token", {}).get("refreshed") != today.isoformat():
            jobs.append(("トークン延長", refresh))
    failed = []
    for name, job in jobs:
        try:
            job()
        except Exception as ex:  # 1つ失敗しても、ほかは進める
            print(f"失敗：{name} {ex}")
            failed.append(name)
    print(f"{t:%m/%d %H:%M} 確認した作業：{[n for n, _ in jobs]}")
    if failed:
        sys.exit(1)


# ---------- テスト（投稿はしない） ----------
def test():
    e = read_json("schedule.json", {"schedule": []})["schedule"][0]
    p = post_data(e)
    imgs = "\n".join(f'<img src="{raw(f"posts/{e["id"]}/post_{n}.jpg")}" width="320">' for n in range(1, p["slides"] + 1))
    body = (f"@{OWNER} これは**テスト**です。投稿はされません。\n\n"
            f"確かめたら、下のコメント欄に **「はい」** と書いて送ってください。\n\n### 5枚の画像\n{imgs}\n")
    issue = gh("POST", f"repos/{REPO}/issues", {"title": f"【テスト】確認ページの練習（{p['theme']}）", "body": body})
    state_update("_test", {"issue": issue["number"]}, "テスト")
    print("テストの確認ページ #", issue["number"])


def testcheck():
    st, _ = state_get()
    issue = st.get("_test", {}).get("issue")
    if not issue:
        print("テストなし")
        return
    ans = decision(issue)
    comment(issue, f"テストの結果：返事を「{ans or 'まだなし'}」と受け取りました。本番でも同じように読み取ります。")
    if ans:
        gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed"})
    print("テストの返事:", ans)


if __name__ == "__main__":
    {"tick": tick, "notify": notify, "post": post, "refresh": refresh,
     "test": test, "testcheck": testcheck}[sys.argv[1]]()
