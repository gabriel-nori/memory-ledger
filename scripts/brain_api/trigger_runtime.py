"""The trigger watchers, running inside the server (docs/trigger.md).

Everything that decides, logs and dispatches is in `scripts/trigger.py`, which
is stdlib. This module only owns what the server has and the engine may not:
the Slack Socket Mode websocket (`slack_sdk`), the Jira, GitHub and B2B poll loops
(docs/b2b.md), the custom triggers' scheduler (`custom_trigger.py`), and the pool
the headless sessions run on — so several mentions run at once without a watcher
waiting on any of them.

Started from the app's lifespan when `state.TRIGGERS` is set, which `serve.py`
leaves off under `--no-trigger` and `--no-write`: a read-only server must not
be acting on its owner's behalf.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import b2b
import custom_trigger
import inbound_http_trigger
import trigger

log = trigger.log

# While the socket is up every mention arrives live, so the Slack checkpoint
# can follow the clock. It trails by a margin, so a mention in flight at the
# moment of a crash is still inside the next catch-up window.
SLACK_HEARTBEAT_S = 30
SLACK_TRAIL_S = 120
# How often the scheduler looks for a custom trigger that is due. Each trigger
# keeps its own interval; this is only how late a due one can start.
CUSTOM_TICK_S = 15

_lock = threading.Lock()
# Serialises start / stop / restart: two saves from the page at once must not
# leave two sets of watchers running.
_life = threading.Lock()
# One event per start. A restart gives the new watchers a fresh one rather than
# clearing the old, so a loop from before the restart cannot miss its stop.
_stop = threading.Event()
_pool: ThreadPoolExecutor | None = None
_slack_client = None
STATE: dict = {"running": False, "reason": "not started", "started_at": "",
               "slack": {"state": "off"}, "jira": {"state": "off"}, "github": {"state": "off"},
               "b2b": {"state": "off"}, "custom": {"state": "off"}, "inbound_http": {"state": "off"},
               "recovered": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _note(source: str, **fields) -> None:
    with _lock:
        before = STATE.get(source, {})
        STATE[source] = {**before, **fields, "at": _now()}
    # A change of state, or a new error, is worth a line; a poll that went as
    # the last one did is not.
    state, err = fields.get("state"), fields.get("last_error")
    if state and state != before.get("state"):
        (log.warning if state in ("erroring", "misconfigured", "unavailable") else log.info)(
            "%s: %s%s", source, state, f" — {err}" if err else "")
    elif err and err != before.get("last_error"):
        log.warning("%s: %s", source, err)


def _summary(cfg: dict, workers: int) -> str:
    on = []
    for src in trigger.SOURCES:
        if not trigger.source_enabled(cfg, src):
            continue
        every = (cfg.get(src) or {}).get("poll_seconds")
        on.append(f"{src} (poll {every}s)" if src != "slack" and every else src)
    if trigger.source_enabled(cfg, "custom"):
        on.append("custom triggers")
    if trigger.source_enabled(cfg, "inbound_http"):
        on.append("inbound HTTP triggers")
    return (f"sources: {', '.join(on) or 'none'}; up to {workers} sessions at once; "
            f"log: {trigger.log_path()}")


def _dispatch(request_id: str) -> None:
    if _pool is not None:
        _pool.submit(_run, request_id)


def run_soon(request_id: str) -> str:
    """Run a request the owner asked for by hand (a reprocess): on the pool
    when the watchers are up, else on a thread of its own, so it runs even
    with the triggers off. Says which."""
    if _pool is not None:
        _pool.submit(_run, request_id)
        return "pool"
    threading.Thread(target=_run, args=(request_id,), name=f"reprocess-{request_id[:8]}",
                     daemon=True).start()
    return "thread"


def _notice(request_id: str, body: str) -> None:
    """A refusal notice, on the pool: translating one is a model call, and a
    watcher must not wait on it."""
    if _pool is not None:
        _pool.submit(_deliver, request_id, body)


def _deliver(request_id: str, body: str) -> None:
    try:
        trigger.deliver_notice(request_id, body)
    except Exception:
        log.exception("notice for %s crashed", request_id)


def _run(request_id: str) -> None:
    try:
        trigger.run_request(request_id)
    except Exception:
        log.exception("task %s crashed", request_id)


def status() -> dict:
    with _lock:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in STATE.items()}


def start(*, recovering: bool = True) -> None:
    """Non-blocking: the network calls happen on the watchers' own threads, so
    a slow Slack never holds up the server's start."""
    with _life:
        _start(recovering)


def _start(recovering: bool) -> None:
    global _pool, _stop
    trigger.setup_logging()
    cfg = trigger.load_config()
    STATE.update(slack={"state": "off"}, jira={"state": "off"}, github={"state": "off"},
                 b2b={"state": "off"}, custom={"state": "off"}, inbound_http={"state": "off"})
    if not cfg.get("enabled") or cfg.get("_problem"):
        STATE.update(running=False, reason=cfg.get("_problem") or "triggers are switched off")
        log.info("triggers not running: %s", STATE["reason"])
        return
    _stop = threading.Event()
    workers = max(1, min(16, int((cfg.get("claude") or {}).get("max_concurrent") or 3)))
    _pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="trigger-run")
    STATE.update(running=True, reason="", started_at=_now())
    log.info("triggers running — %s", _summary(cfg, workers))
    if recovering:
        # Only when the process starts. On a restart the sessions still
        # running belong to this very process and are not orphans.
        STATE["recovered"] = trigger.recover(cfg, _dispatch)
    if trigger.source_enabled(cfg, "jira"):
        threading.Thread(target=_jira_loop, args=(cfg, _stop), daemon=True,
                         name="trigger-jira").start()
    if trigger.source_enabled(cfg, "github"):
        threading.Thread(target=_github_loop, args=(cfg, _stop), daemon=True,
                         name="trigger-github").start()
    if trigger.source_enabled(cfg, "custom"):
        threading.Thread(target=_custom_loop, args=(cfg, _stop), daemon=True,
                         name="trigger-custom").start()
    if trigger.source_enabled(cfg, "inbound_http"):
        threading.Thread(target=_inbound_loop, args=(cfg, _stop), daemon=True,
                         name="trigger-inbound-http").start()
    b2b_cfg = b2b.load_config()
    if b2b.active(b2b_cfg):
        # Its own file and switch, under the same master switch: a message
        # from another brain runs a session on the owner's behalf too.
        threading.Thread(target=_b2b_loop, args=(b2b_cfg, _stop), daemon=True,
                         name="trigger-b2b").start()
    elif b2b_cfg.get("enabled"):
        _note("b2b", state="misconfigured",
              last_error=b2b_cfg.get("_problem")
              or "needs an identity and an enabled broker")
    if trigger.source_enabled(cfg, "slack"):
        threading.Thread(target=_slack_main, args=(cfg, _stop), daemon=True,
                         name="trigger-slack").start()


def stop() -> None:
    with _life:
        _stop_now()


def _stop_now() -> None:
    global _pool, _slack_client
    _stop.set()
    if _slack_client is not None:
        try:
            _slack_client.close()
        except Exception:
            pass
        _slack_client = None
    if _pool is not None:
        # Do not wait: a session may run for minutes, and it is a child that
        # outlives this process anyway. recover() marks it on the next start.
        _pool.shutdown(wait=False, cancel_futures=False)
        _pool = None
    if STATE.get("running"):
        log.info("triggers stopped")
    STATE.update(running=False, reason="stopped")


def restart() -> dict:
    """Stop and start again on the config as it now is. What the page calls
    after saving, so a change applies without restarting the server."""
    with _life:
        _stop_now()
        _start(recovering=False)
    return status()


# ---------------------------------------------------------------- jira
def _jira_loop(cfg: dict, stop_event: threading.Event) -> None:
    every = max(15, int((cfg.get("jira") or {}).get("poll_seconds") or 60))
    jira = trigger.Jira(cfg)
    _note("jira", state="polling", every_s=every)
    while not stop_event.is_set():
        try:
            res = trigger.jira_poll(cfg, _dispatch, jira=jira, notice=_notice)
            if res.get("ok"):
                _note("jira", state="polling", last_ok=_now(), last_error="",
                      last={k: res.get(k) for k in ("issues", "seen", "handled", "first_run")})
            else:
                _note("jira", state="misconfigured", last_error=res.get("error", ""))
        except Exception as e:
            _note("jira", state="erroring", last_error=str(e)[:300])
        stop_event.wait(every)


# ---------------------------------------------------------------- github
def _github_loop(cfg: dict, stop_event: threading.Event) -> None:
    every = max(30, int((cfg.get("github") or {}).get("poll_seconds") or 120))
    gh = trigger.GitHub(cfg)
    _note("github", state="polling", every_s=every)
    while not stop_event.is_set():
        try:
            res = trigger.github_poll(cfg, _dispatch, gh=gh, notice=_notice)
            if res.get("ok"):
                _note("github", state="polling", last_ok=_now(), last_error="",
                      last={k: res.get(k) for k in ("items", "seen", "handled", "first_run")})
            else:
                _note("github", state="misconfigured", last_error=res.get("error", ""))
        except Exception as e:
            _note("github", state="erroring", last_error=str(e)[:300])
        stop_event.wait(every)


# ---------------------------------------------------------------- custom
def _custom_loop(cfg: dict, stop_event: threading.Event, tick: float = CUSTOM_TICK_S) -> None:
    """Every `tick`, run each enabled custom trigger that is due, one after
    the other. The files are read on every pass, so an edit or an enable
    counts without a restart; a broken one is noted in its own state and
    skipped, and the loop goes on."""
    _note("custom", state="scheduling", every_s=tick)
    while not stop_event.is_set():
        try:
            ran = custom_trigger.run_due(cfg, _dispatch)
            errors = [f"{r['id']}: {r.get('error')}" for r in ran if r.get("status") == "error"]
            _note("custom", state="scheduling", last_ok=_now(),
                  last_error="; ".join(errors)[:300],
                  last={"ran": len(ran), "tasks": sum(len(r.get("created") or []) for r in ran)})
        except Exception as e:
            _note("custom", state="erroring", last_error=str(e)[:300])
        stop_event.wait(tick)


# ---------------------------------------------------------------- inbound http
def _inbound_loop(cfg: dict, stop_event: threading.Event, tick: float = CUSTOM_TICK_S) -> None:
    """Keep one listener open per enabled inbound-http trigger: a file switched on, off
    or moved to another port counts within a tick. Closed when the triggers stop."""
    mgr = inbound_http_trigger.Manager(_dispatch)
    _note("inbound_http", state="listening", every_s=tick)
    try:
        while not stop_event.is_set():
            try:
                res = mgr.sync()
                _note("inbound_http", state="listening", last_ok=_now(), listening=res["listening"],
                      last_error="; ".join(f"{k}: {v}" for k, v in res["errors"].items())[:300])
            except Exception as e:
                _note("inbound_http", state="erroring", last_error=str(e)[:300])
            stop_event.wait(tick)
    finally:
        mgr.stop()


# ---------------------------------------------------------------- b2b
def _b2b_loop(cfg: dict, stop_event: threading.Event) -> None:
    """Read the relay every `poll_seconds`. Each message is deleted there as
    it is read, so whatever arrives is logged as a request before anything
    else (`b2b.poll`)."""
    every = b2b.poll_seconds(cfg)
    _note("b2b", state="polling", every_s=every)
    while not stop_event.is_set():
        try:
            res = b2b.poll(cfg, _dispatch)
            if res.get("ok"):
                _note("b2b", state="polling", last_ok=_now(), last_error="",
                      last={k: len(res.get(k) or []) for k in ("queued", "refused")})
            else:
                _note("b2b", state="erroring", last_error=res.get("error", ""))
        except Exception as e:
            _note("b2b", state="erroring", last_error=str(e)[:300])
        stop_event.wait(every)


# ---------------------------------------------------------------- slack
def _slack_main(cfg: dict, stop_event: threading.Event) -> None:
    global _slack_client
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web import WebClient
    except ImportError as e:
        _note("slack", state="unavailable", last_error=f"slack_sdk is not installed: {e}")
        return
    app_token = trigger.secret(cfg, "slack", "app_token")
    bot_token = trigger.secret(cfg, "slack", "bot_token")
    if not (app_token and bot_token):
        _note("slack", state="misconfigured", last_error="needs an app token and a bot token")
        return
    try:
        bot = trigger.slack_bot_user_id(cfg)
    except trigger.TriggerError as e:
        _note("slack", state="misconfigured", last_error=str(e))
        return
    notify = trigger.slack_notify(cfg, _notice)
    dms = bool((cfg.get("slack") or {}).get("dms")) and bool(trigger.secret(cfg, "slack", "user_token"))

    def on_mention(event: dict) -> None:
        try:
            ev = trigger.slack_event(event, event.get("channel", ""), cfg, bot_user_id=bot)
            res = trigger.handle(ev, cfg, _dispatch, notify)
            if ev:
                with trigger.db() as con:
                    trigger.set_checkpoint(con, "slack", ev["meta"]["ts"])
            _note("slack", last_event=_now(), last_action=res.get("action"))
        except Exception as e:
            _note("slack", last_error=str(e)[:300])

    def on_member(event: dict) -> None:
        try:
            ev = trigger.slack_member_event(event, cfg, bot_user_id=bot)
            res = trigger.handle(ev, cfg, _dispatch)
            if ev:
                _note("slack", last_event=_now(), last_action=res.get("action"))
        except Exception as e:
            _note("slack", last_error=str(e)[:300])

    def listener(client, req) -> None:
        if req.type != "events_api":
            return
        # Ack first: Slack redelivers what is not acked within 3s, and the
        # permalink lookup alone can take longer than that.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        event = (req.payload or {}).get("event") or {}
        if event.get("type") == "app_mention":
            threading.Thread(target=on_mention, args=(event,), daemon=True).start()
        elif (event.get("type") == "message" and dms
              and event.get("channel_type") in trigger.DM_TYPES
              and trigger.mentions_bot(event.get("text") or "", bot)):
            # One of the owner's DMs, heard through their user token: the bot
            # is never in it, so no app_mention comes for it.
            threading.Thread(target=on_mention, args=(event,), daemon=True).start()
        elif event.get("type") in trigger.SLACK_MEMBER_EVENTS and event.get("user") == bot:
            threading.Thread(target=on_member, args=(event,), daemon=True).start()

    client = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot_token))
    client.socket_mode_request_listeners.append(listener)
    _slack_client = client
    _note("slack", state="connecting", bot_user_id=bot)
    try:
        client.connect()
    except Exception as e:
        _note("slack", state="erroring", last_error=str(e)[:300])
    was_connected = False
    while not stop_event.is_set():
        up = False
        try:
            up = client.is_connected()
        except Exception:
            pass
        if up and not was_connected:
            # (Re)connected: whatever arrived while we were away is in history.
            _note("slack", state="connected", connected_at=_now())
            try:
                res = trigger.slack_catch_up(cfg, _dispatch, notice=_notice)
                _note("slack", last_catch_up={k: res.get(k) for k in
                                              ("channels", "seen", "handled", "first_run")})
            except Exception as e:
                _note("slack", last_error=f"catch-up: {e}"[:300])
        elif up:
            with trigger.db() as con:
                trigger.set_checkpoint(con, "slack", f"{time.time() - SLACK_TRAIL_S:.6f}")
        elif was_connected:
            _note("slack", state="reconnecting")
        was_connected = up
        stop_event.wait(SLACK_HEARTBEAT_S if up else 5)
    try:
        client.close()
    except Exception:
        pass
