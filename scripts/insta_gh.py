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
        # 制限の原因を調べられるように、使用量のヘッダーも残す
        usage = {k: e.headers.get(k) for k in ("X-App-Usage", "X-Business-Use-Case-Usage") if e.headers.get(k)}
        print("エラーの詳細：", e.code, usage)
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


def published_today(ig_user, caption, media_type, day):
    """今日、同じ1行目の投稿がインスタに出ていれば返す。無ければ None。"""
    first = caption.split("\n")[0].strip()
    items = ig("GET", f"{ig_user}/media", {"fields": "id,caption,media_type,timestamp", "limit": 15}).get("data", [])
    for m in items:
        at = dt.datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(JST)
        if at.date() == day and m.get("media_type") == media_type and (m.get("caption") or "").split("\n")[0].strip() == first:
            return m
    return None


def publish_checked(ig_user, creation_id, caption, media_type, day):
    """投稿する。エラーが返っても投稿は出ていることがある（2026-10-27〜30 に実際に起きた）ので、
    失敗と決める前に、インスタの投稿一覧で確かめる（同じ投稿を2回出さないため）。"""
    # 先に確かめる。すでに出ていれば投稿しない。一覧を読めない時も、二重を避けるため投稿しない（ここで止まる）
    found = published_today(ig_user, caption, media_type, day)
    if found:
        print("今日の分はもう出ていたので、投稿しません", found["id"])
        return found
    try:
        return ig("POST", f"{ig_user}/media_publish", {"creation_id": creation_id})
    except Exception as ex:
        print("投稿の返事がエラー。実際に出たか確かめます：", str(ex)[:200])
        for wait in (20, 40, 60):
            time.sleep(wait)
            try:
                found = published_today(ig_user, caption, media_type, day)
            except Exception as check:
                print("投稿一覧を読めませんでした：", str(check)[:200])
                continue
            if found:
                print("エラーでしたが、投稿は出ていました", found["id"])
                return found
        raise ex


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
    try:
        # 前の回で作った箱があれば使い回す（何度も作り直すと、インスタに止められやすい）
        parent_id = s.get("container")
        if not parent_id:
            children = []
            for n in range(1, p["slides"] + 1):
                c = ig("POST", f"{ig_user}/media", {"image_url": raw(f"posts/{e['id']}/post_{n}.jpg"), "is_carousel_item": "true"})
                wait_container(c["id"])
                children.append(c["id"])
            parent = ig("POST", f"{ig_user}/media", {"media_type": "CAROUSEL", "children": ",".join(children),
                                                      "caption": p["caption"] + "\n\n" + " ".join(p["hashtags"])})
            wait_container(parent["id"])
            parent_id = parent["id"]
            state_update(e["id"], {"container": parent_id}, f"{e['id']} 投稿の箱")
        media = publish_checked(ig_user, parent_id, p["caption"], "CAROUSEL_ALBUM", day)
    except Exception as ex:
        give_up(e, s, issue, ex, "post")
        return
    link = ig("GET", media["id"], {"fields": "permalink"}).get("permalink", "")
    state_update(e["id"], {"status": "投稿済み", "media_id": media["id"], "permalink": link,
                           "posted_at": now().isoformat(timespec="seconds")}, f"{e['id']} 投稿済み")
    if issue:
        comment(issue, f"投稿しました ✅ {link}")
        gh("PATCH", f"repos/{REPO}/issues/{issue}", {"state": "closed", "labels": ["投稿済み"]})
    print("投稿完了", link)


# ---------- リール（昼12時以降・1回だけ） ----------
def post_reel(day=None):
    """承認済みの日だけ、同じ内容のリールを投稿する（フィードには出さず、リールのタブに出す）。"""
    day = day or now().date()
    e = entry_for(day)
    if not e:
        return
    p = post_data(e)
    if not p.get("reel"):
        return
    st, _ = state_get()
    s = st.get(e["id"], {})
    if s.get("reel_status") in ("投稿済み", "失敗") or s.get("closed"):
        return
    if s.get("reel_posting"):
        if now() - dt.datetime.fromisoformat(s["reel_posting"]) < dt.timedelta(minutes=30):
            return
    ok = s.get("status") in ("承認済み", "投稿済み") and (not s.get("issue") or decision(s["issue"]) == "はい")
    if not ok:
        return  # 「はい」がまだなら、まだ出さない（19時までに「はい」が来れば、その後の回で出す）
    state_update(e["id"], {"reel_posting": now().isoformat(timespec="seconds")}, f"{e['id']} リール開始")
    ig_user = os.environ["IG_USER_ID"]
    try:
        c = ig("POST", f"{ig_user}/media", {"media_type": "REELS", "video_url": raw(f"posts/{e['id']}/reel.mp4"),
                                             "caption": reel_caption(p),
                                             "share_to_feed": "false"})
        wait_container(c["id"], limit=900)
        media = publish_checked(ig_user, c["id"], p["caption"], "VIDEO", day)
        link = ig("GET", media["id"], {"fields": "permalink"}).get("permalink", "")
        state_update(e["id"], {"reel_status": "投稿済み", "reel_id": media["id"], "reel_permalink": link},
                     f"{e['id']} リール投稿済み")
        if s.get("issue"):
            comment(s["issue"], f"リールも投稿しました 🎬 {link}")
        print("リール投稿完了", link)
    except Exception as ex:
        state_update(e["id"], {"reel_status": "失敗", "reel_error": str(ex)[:300]}, f"{e['id']} リール失敗")
        if s.get("issue"):
            comment(s["issue"], f"@{OWNER} リールの投稿がうまくいきませんでした（{why_ja(ex)}）。今日のリールはもう試しません。19時の画像の投稿は、いつもどおり進めます。")
        print("リール失敗", ex)


def reel_caption(p):
    """リールの文章は、画像の投稿と同じにしない（同じ日に同じ文・同じタグを2回出すと、インスタに止められやすい）。
    1行目（表紙の言葉）＋夜の画像の案内だけにして、タグもテーマの3つ＋#おむすび先生 の4つにしぼる。"""
    first = p["caption"].split("\n")[0]
    tags = [h for h in p["hashtags"][:3]] + ["#おむすび先生"]
    return (f"{first}\n\n今夜19時に、5枚の図解でくわしく投稿します。\n"
            f"気になったら、プロフィールから見てね。\n\n{' '.join(dict.fromkeys(tags))}")


def why_ja(ex):
    m = str(ex)
    if "2207051" in m or "action is blocked" in m or "request limit" in m:
        return "インスタ側で、操作が一時的に制限されました"
    return "インスタからエラーが返ってきました"


def give_up(e, s, issue, ex, kind):
    """投稿の失敗。インスタに止められたら、その日はもう試さない（やり直すほど制限が長引くため）。
    それ以外の失敗は、3回まで（20分あけて）やり直す。どちらも、確認ページに1回だけ知らせる。"""
    m = str(ex)
    blocked = "2207051" in m or "action is blocked" in m or "request limit" in m
    tries = s.get("post_tries", 0) + 1
    stop = blocked or tries >= 3
    changes = {"post_tries": tries, "post_error": m[:300], "posting": None}
    if stop:
        changes.update({"status": "インスタに止められた" if blocked else "投稿失敗", "closed": True})
    state_update(e["id"], changes, f"{e['id']} 投稿失敗 {tries}回目")
    print(f"投稿失敗（{tries}回目）：{m[:200]}")
    if stop and issue:
        if blocked:
            msg = (f"@{OWNER} インスタ側で投稿が一時的に制限されたため、**今日の画像の投稿は見送りました**。\n\n"
                   "何度もやり直すと制限が長引くので、今日はもう試しません。明日の分は、いつもどおり進めます。\n"
                   "インスタのアプリに「アクションがブロックされました」などのお知らせが出ていないか、見ておいてください。")
        else:
            msg = f"@{OWNER} 3回試しましたが、今日の画像を投稿できませんでした。Claude に「投稿の失敗」と伝えてください。\n\n`{m[:200]}`"
        comment(issue, msg)


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


# ---------- 作り置きの見張り（1日1回） ----------
STOCK_MIN = 3


def stock_check():
    """明日から何日分の投稿が GitHub にそろっているかを数え、STOCK_MIN 日を切ったら知らせる。"""
    t = now()
    st, _ = state_get()
    if st.get("_stock", {}).get("checked") == t.date().isoformat():
        return
    days = 0
    d = t.date() + dt.timedelta(days=1)
    while True:
        e = entry_for(d)
        if not e or not (ROOT / "posts" / e["id"] / "post.json").exists():
            break
        days += 1
        d += dt.timedelta(days=1)
    if days < STOCK_MIN:
        empty = t.date() + dt.timedelta(days=days + 1)
        body = (f"@{OWNER} **作り置きが残り{days}日分**です。このままだと **{label(empty)}** から投稿が止まります。\n\n"
                f"考えられる原因：Mac の画像づくりが止まっている／Mac をしばらく開いていない／原稿が足りない\n\n"
                f"Mac を開いて、Claude に「作り置きが足りない」と伝えてください。\n"
                f"（この知らせは1日1回、作り置きが{STOCK_MIN}日分になるまで届きます）")
        gh("POST", f"repos/{REPO}/issues", {"title": f"【お知らせ】作り置きが残り{days}日分です", "body": body,
                                              "labels": ["お知らせ"]})
        print(f"作り置き不足を通知（残り{days}日）")
    state_update("_stock", {"checked": t.date().isoformat(), "days": days}, f"作り置き {days}日分")


# ---------- 数字の記録（1日1回） ----------
METRICS = "reach,views,saved,likes,shares,comments"


def record_insights():
    """投稿済みの投稿（とリール）の数字と、フォロワー数を state.json に記録する。直近30日分。"""
    t = now()
    st, _ = state_get()
    if st.get("_insights", {}).get("date") == t.date().isoformat():
        return
    me = ig("GET", "me", {"fields": "followers_count,media_count"})
    since = (t.date() - dt.timedelta(days=30)).isoformat()
    for k, s in st.items():
        if k.startswith("_") or s.get("date", "") < since:
            continue
        rec = {}
        for kind, mid in (("post", s.get("media_id")), ("reel", s.get("reel_id"))):
            if not mid:
                continue
            try:
                d = ig("GET", f"{mid}/insights", {"metric": METRICS})
                rec[kind] = {x["name"]: x["values"][0]["value"] for x in d.get("data", [])}
            except Exception as ex:
                rec[kind] = {"error": str(ex)[:120]}
        if rec:
            rec["at"] = t.isoformat(timespec="minutes")
            state_update(k, {"insights": rec}, f"{k} 数字")
    state_update("_insights", {"date": t.date().isoformat(), "followers": me.get("followers_count"),
                               "media": me.get("media_count")}, "数字の記録")
    print("数字を記録した：フォロワー", me.get("followers_count"))


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
    if t.hour >= 12:
        jobs.append(("今日分のリール", lambda: post_reel(today)))
    if t.hour >= 19:
        jobs.append(("今日分の投稿", lambda: post(today)))
    if t.hour >= 12:
        jobs.append(("作り置きの見張り", stock_check))
    if t.hour >= 21:
        jobs.append(("数字の記録", record_insights))
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
