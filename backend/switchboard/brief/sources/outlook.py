"""Mail from an Outlook inbox, read through Microsoft Graph.

Signing in uses Microsoft's device code flow with an app registration of your own, a public client
with no secret. The permission asked for is Mail.Read, which can read mail and can't send, delete or
change anything. The refresh token that sign-in leaves is kept in the credential store and renewed on
every build. Access tokens live in memory for one build only.

Each build reads the inbox and Sent Items for the last `lookback_days`. A conversation where you
replied after its last message is handled, and so is one whose last message you flagged complete.
The rest are sorted by a local model, once per message (triage.py). Mail it says needs nothing is
left off the list.

Tokens go to Microsoft's sign-in and Graph and nowhere else, and no error message carries one.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from ... import credentials
from ...config import BriefMailCfg, Config
from ...providers import OllamaClient
from .. import store
from ..base import Found, SourceError, Usage, stamp
from ..drafts import Drafter, Message
from ..triage import Mail, ModelUnavailable, Sorter, pick_model

log = logging.getLogger("switchboard.brief")

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_TOKEN = "outlook_refresh_token"  # the name the sign-in is saved under
SETUP = "python -m switchboard brief connect outlook"
# Where a message's own "open in Outlook" link may go. Anything else is dropped.
OUTLOOK_HOSTS = {"outlook.live.com", "outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft"}
TIMEOUT_S = 20
PAGE = 50
INBOX_FIELDS = "id,conversationId,subject,from,receivedDateTime,bodyPreview,webLink,flag,inferenceClassification"
SENT_FIELDS = "conversationId,sentDateTime"
THREAD_FIELDS = "subject,from,receivedDateTime,sentDateTime,uniqueBody,isDraft,parentFolderId"
IMMUTABLE_IDS = 'IdType="ImmutableId"'  # keeps a message's id the same when it is moved to another folder
BODY_AS_TEXT = 'outlook.body-content-type="text"'  # message text as plain text, not HTML

# A build and a draft at the same time would each renew the sign-in and could save them in the wrong order.
_sign_in = threading.Lock()


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S)


def _time(value: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# Signing in ----------------------------------------------------------------------


def explain(body: dict) -> str:
    """What a refusal from Microsoft's sign-in means, with its AADSTS code when it has one. Never
    the description itself, which is long and can carry ids."""
    error = str(body.get("error") or "")
    description = str(body.get("error_description") or "")
    code = re.search(r"AADSTS\d+", description)
    tag = f" ({code.group(0)})" if code else f" ({error})" if error else ""
    if "AADSTS7000218" in description:
        hint = 'Set "Allow public client flows" to Yes under Authentication in the app registration.'
    elif "AADSTS700016" in description or error == "unauthorized_client":
        hint = "Check brief.mail.client_id, and that the app registration allows personal Microsoft accounts."
    elif error == "invalid_grant":
        hint = f"The Outlook sign-in has expired or was taken back. Sign in again with: {SETUP}"
    elif error == "invalid_scope" or "AADSTS70011" in description:
        hint = "The app registration needs the delegated Microsoft Graph permission Mail.Read."
    else:
        hint = "Microsoft's sign-in refused."
    return hint + tag


class SignIn:
    """Microsoft sign-in for a public client: the device code flow first, refresh tokens after."""

    def __init__(self, cfg: BriefMailCfg, http: httpx.Client):
        self.cfg = cfg
        self.http = http

    def _post(self, endpoint: str, form: dict) -> tuple[int, dict]:
        url = f"{LOGIN}/{quote(self.cfg.tenant, safe='')}/oauth2/v2.0/{endpoint}"
        try:
            r = self.http.post(url, data={**form, "client_id": self.cfg.client_id or ""})
        except httpx.TimeoutException:
            raise SourceError("Microsoft's sign-in took too long to answer.") from None
        except httpx.HTTPError:
            raise SourceError("Microsoft's sign-in couldn't be reached.") from None
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body if isinstance(body, dict) else {}

    @staticmethod
    def _checked(tokens: dict) -> dict:
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise SourceError("Microsoft signed you in but didn't hand over a lasting sign-in.")
        if "scope" in tokens and "mail.read" not in str(tokens["scope"]).lower():
            raise SourceError("Microsoft signed you in without permission to read mail. "
                              "The app registration needs the delegated permission Mail.Read.")
        return tokens

    def start(self) -> dict:
        """Ask for a code. The answer says where to enter it: `verification_uri` and `user_code`."""
        status, body = self._post("devicecode", {"scope": SCOPE})
        if status != 200 or not all(body.get(k) for k in ("device_code", "user_code", "verification_uri")):
            raise SourceError(explain(body))
        return body

    def wait(self, flow: dict, sleep=None, clock=None) -> dict:
        """Ask Microsoft every few seconds until the code has been used, then return the tokens."""
        sleep, clock = sleep or time.sleep, clock or time.monotonic
        interval = max(int(flow.get("interval") or 5), 1)
        expires = flow.get("expires_in")
        deadline = clock() + (int(expires) if expires is not None else 900)  # 0 means it has already run out
        while clock() < deadline:
            sleep(interval)
            status, body = self._post("token", {"grant_type": DEVICE_GRANT, "device_code": flow["device_code"]})
            if status == 200 and body.get("access_token"):
                return self._checked(body)
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in ("authorization_declined", "access_denied"):
                raise SourceError("The sign-in was declined.")
            if error in ("expired_token", "code_expired"):
                break
            raise SourceError(explain(body))
        raise SourceError("The code expired before the sign-in finished.")

    def refresh(self, refresh_token: str) -> dict:
        status, body = self._post("token", {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                            "scope": SCOPE})
        if status == 200 and body.get("access_token"):
            return self._checked(body)
        raise SourceError(explain(body))


# Reading mail --------------------------------------------------------------------


class Mailbox:
    """Read-only calls to Microsoft Graph, with one access token."""

    def __init__(self, access_token: str, http: httpx.Client):
        self.token = access_token
        self.http = http

    def _get(self, url: str, prefer: str = IMMUTABLE_IDS) -> dict:
        if not url.startswith(GRAPH + "/"):  # the token only ever goes to Graph
            raise SourceError("Outlook pointed at an address outside Microsoft Graph.")
        try:
            r = self.http.get(url, headers={"Authorization": f"Bearer {self.token}", "Prefer": prefer})
        except httpx.TimeoutException:
            raise SourceError("Outlook took too long to answer.") from None
        except httpx.HTTPError:
            raise SourceError("Outlook couldn't be reached.") from None
        if r.status_code == 401:
            raise SourceError(f"Outlook wants you to sign in again. Run: {SETUP}")
        if r.status_code == 403:
            raise SourceError("Outlook refused to share the mail. The app registration needs the permission Mail.Read.")
        if r.status_code == 429 or r.status_code >= 500:
            raise SourceError("Outlook is busy right now. The next build tries again.")
        if r.status_code != 200:
            raise SourceError(f"Outlook answered with HTTP {r.status_code}.")
        try:
            data = r.json()
        except ValueError:
            raise SourceError("Outlook sent something that isn't JSON.") from None
        return data if isinstance(data, dict) else {}

    def messages(self, folder: str, field: str, since: datetime, limit: int, fields: str) -> tuple[list[dict], bool]:
        """Messages in a folder from `since` on, newest first, following Graph's pages up to `limit`,
        and whether there were more than that."""
        since_text = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = "&".join(f"{key}={quote(value, safe=',')}" for key, value in (
            ("$filter", f"{field} ge {since_text}"),
            ("$orderby", f"{field} desc"),  # Graph wants the ordered field in the filter, first
            ("$top", str(min(PAGE, limit))),
            ("$select", fields),
        ))
        url: str | None = f"{GRAPH}/me/mailFolders/{folder}/messages?{query}"
        found: list[dict] = []
        while url and len(found) < limit:
            page = self._get(url)
            found.extend(m for m in page.get("value") or [] if isinstance(m, dict))
            url = page.get("@odata.nextLink")
        return found[:limit], len(found) > limit or bool(url)

    def thread(self, conversation_id: str, limit: int = 25) -> list[dict]:
        """The messages of one conversation, in every folder, each with only the text it added
        (`uniqueBody`, without the quoted messages before it), as plain text."""
        literal = conversation_id.replace("'", "''")  # a quote inside an OData string is doubled
        query = "&".join(f"{key}={quote(value, safe=',')}" for key, value in (
            ("$filter", f"conversationId eq '{literal}'"),
            ("$select", THREAD_FIELDS),
            ("$top", str(limit)),
        ))
        page = self._get(f"{GRAPH}/me/messages?{query}", prefer=BODY_AS_TEXT)
        return [m for m in page.get("value") or [] if isinstance(m, dict)]

    def folder_id(self, name: str) -> str | None:
        """The id of a well-known folder such as sentitems, to tell which messages are yours."""
        return self._get(f"{GRAPH}/me/mailFolders/{name}?$select=id", prefer=BODY_AS_TEXT).get("id")


@dataclass
class Conversation:
    id: str
    latest: dict  # its newest message in the inbox
    received_at: datetime
    count: int  # its messages in the inbox within the window
    replied_at: datetime | None  # your newest reply, when it came after `received_at`
    flagged_done: bool

    @property
    def handled(self) -> bool:
        return self.replied_at is not None or self.flagged_done


def conversations(inbox: list[dict], sent: list[dict]) -> list[Conversation]:
    """The inbox grouped by conversation, newest first, with what you did about each."""
    last_sent: dict[str, datetime] = {}
    for m in sent:
        when, cid = _time(m.get("sentDateTime")), m.get("conversationId")
        if when and cid and (cid not in last_sent or when > last_sent[cid]):
            last_sent[cid] = when
    grouped: dict[str, Conversation] = {}
    for m in inbox:
        when, cid = _time(m.get("receivedDateTime")), m.get("conversationId")
        if not when or not cid or not m.get("id"):
            continue
        c = grouped.get(cid)
        if c is None:
            grouped[cid] = Conversation(cid, m, when, 1, None, False)
        else:
            c.count += 1
            if when > c.received_at:
                c.latest, c.received_at = m, when
    for c in grouped.values():
        replied = last_sent.get(c.id)
        c.replied_at = replied if replied and replied > c.received_at else None
        c.flagged_done = (c.latest.get("flag") or {}).get("flagStatus") == "complete"
    return sorted(grouped.values(), key=lambda c: c.received_at, reverse=True)


def sender(message: dict) -> tuple[str, str]:
    """(name, address) of who sent it."""
    who = (message.get("from") or {}).get("emailAddress") or {}
    return str(who.get("name") or "").strip(), str(who.get("address") or "").strip()


def own_link(message: dict) -> str | None:
    """The message's link to Outlook on the web, but only when it is https and goes to Outlook."""
    link = message.get("webLink")
    try:
        url = httpx.URL(str(link))
    except Exception:
        return None
    return str(link) if link and url.scheme == "https" and url.host.lower() in OUTLOOK_HOSTS else None


@dataclass
class Snapshot:
    """What one read of the mailbox found."""

    conversations: list[Conversation]
    messages: int  # inbox messages read
    sent: int  # messages you sent in the same days
    capped: bool  # there was more mail in those days than max_messages, and the oldest was left out


def thread_messages(raw: list[dict], sent_folder: str | None) -> list[Message]:
    """A conversation as the drafter sees it, oldest first. Yours are the ones in Sent Items, and
    drafts are left out."""
    found: list[Message] = []
    for m in raw:
        if m.get("isDraft"):
            continue
        mine = bool(sent_folder) and m.get("parentFolderId") == sent_folder
        name, address = sender(m)
        when = _time(m.get("sentDateTime") if mine else m.get("receivedDateTime")) or _time(m.get("receivedDateTime"))
        text = str((m.get("uniqueBody") or {}).get("content") or "")
        found.append(Message(mine=mine, sender="you" if mine else name or address or "someone", when=when,
                             subject=str(m.get("subject") or ""), text=text))
    return sorted(found, key=lambda m: m.when or datetime.min.replace(tzinfo=UTC))


def read(cfg: BriefMailCfg, http: httpx.Client, access_token: str, now: datetime) -> Snapshot:
    box = Mailbox(access_token, http)
    since = now - timedelta(days=cfg.lookback_days)
    inbox, capped = box.messages("inbox", "receivedDateTime", since, cfg.max_messages, INBOX_FIELDS)
    sent, _ = box.messages("sentitems", "sentDateTime", since, max(cfg.max_messages * 2, PAGE), SENT_FIELDS)
    return Snapshot(conversations(inbox, sent), len(inbox), len(sent), capped)


def capped_note(cfg: BriefMailCfg) -> str:
    return (f"Only the newest {cfg.max_messages} messages from the last {cfg.lookback_days} days were read. "
            "Raise brief.mail.max_messages to read them all.")


def shape(snapshot: Snapshot, cfg: BriefMailCfg) -> list[str]:
    """What the mailbox looks like, in lines that hold nothing any message says: counts of messages,
    conversations, replies, flags and links. Safe to print and to pass on."""
    found = snapshot.conversations
    focus = [c.latest.get("inferenceClassification") for c in found]
    lines = [f"The inbox has {snapshot.messages} messages from the last {cfg.lookback_days} days, "
             f"in {len(found)} conversations."]
    if snapshot.capped:
        lines.append(f"  {capped_note(cfg)}")
    return lines + [
        f"  sent by you in the same days: {snapshot.sent}",
        f"  answered by you after their last message: {sum(1 for c in found if c.replied_at)}",
        f"  last message flagged complete: {sum(1 for c in found if c.flagged_done)}",
        f"  Focused Inbox: {focus.count('focused')} focused, {focus.count('other')} other, "
        f"{len(focus) - focus.count('focused') - focus.count('other')} not marked",
        f"  with a link that opens in Outlook: {sum(1 for c in found if own_link(c.latest))}",
        f"  with a preview to sort by: {sum(1 for c in found if (c.latest.get('bodyPreview') or '').strip())}",
        f"The brief would ask the local model about {sum(1 for c in found if not c.handled)} of them.",
    ]


# The source ----------------------------------------------------------------------


class OutlookMail:
    name = "mail"
    label = "Outlook"
    setup = SETUP

    def __init__(self, cfg: Config, ollama: OllamaClient):
        self.cfg = cfg
        self.mail = cfg.brief.mail
        self.ollama = ollama
        self.sorter = Sorter(cfg, ollama)
        self.drafter = Drafter(cfg, ollama)

    def connected(self) -> bool:
        return bool(self.mail.client_id) and credentials.get(REFRESH_TOKEN) is not None

    def _access_token(self, http: httpx.Client, usage: Usage | None = None) -> str:
        """An access token for this one job, from the saved sign-in, which is renewed and saved again."""
        if not self.mail.client_id:
            raise SourceError(f"brief.mail.client_id isn't set. Set it in config.local.yaml, then run: {SETUP}")
        with _sign_in:
            refresh_token = credentials.get(REFRESH_TOKEN)
            if not refresh_token:
                raise SourceError(f"Outlook isn't connected. Run: {SETUP}")
            tokens = SignIn(self.mail, http).refresh(refresh_token)
            if tokens["refresh_token"] != refresh_token:
                try:
                    credentials.save(REFRESH_TOKEN, tokens["refresh_token"])
                except credentials.CredentialError:
                    text = "The renewed Outlook sign-in couldn't be saved. If the next build fails, sign in again."
                    if usage is not None:
                        usage.note(text)
                    else:
                        log.warning(text)
        return tokens["access_token"]

    def draft(self, item: dict, now: datetime) -> tuple[str, str]:
        """A reply to the newest message from someone else in the item's conversation, written by the
        local model with your writing notes. Returns (text, model key). Nothing is sent."""
        with _client() as http:
            box = Mailbox(self._access_token(http), http)
            sent_folder = box.folder_id("sentitems")
            messages = thread_messages(box.thread(item["external_id"]), sent_folder)
        if not any(not m.mine for m in messages):
            raise SourceError("There's nothing from anyone else in this conversation to reply to.")
        model_key = pick_model(self.cfg, self.ollama)
        try:
            text, _ = self.drafter.write(messages, self.mail.writing_notes, now, model_key)
        except ModelUnavailable:
            raise SourceError("The local model didn't answer. Is Ollama running?") from None
        return text, model_key

    def collect(self, now: datetime, usage: Usage) -> list[Found]:
        with _client() as http:
            snapshot = read(self.mail, http, self._access_token(http, usage), now)
        if snapshot.capped:
            usage.note(capped_note(self.mail))
        items = self.sort(snapshot.conversations, now, usage)
        store.prune_judgments(stamp(now - timedelta(days=self.mail.lookback_days * 2)))
        return items

    def sort(self, found: list[Conversation], now: datetime, usage: Usage) -> list[Found]:
        """Each conversation as an item, sorted by the local model where it hasn't been before."""
        waiting = [c for c in found if not c.handled]
        known = store.judgments([c.latest["id"] for c in waiting])
        items: list[Found] = []
        model_key: str | None = None
        unavailable = False
        unsorted = 0
        for c in found:
            name, address = sender(c.latest)
            item = {
                "external_id": c.id,
                "title": str(c.latest.get("subject") or "").strip() or "(no subject)",
                "context": name or address or None,
                "received_at": stamp(c.received_at),
                "link": own_link(c.latest),
            }
            if c.handled:
                items.append(Found(**item, done=True, done_at=stamp(c.replied_at) if c.replied_at else None))
                continue
            judged = known.get(c.latest["id"])
            if judged is None and not unavailable:
                model_key = model_key or pick_model(self.cfg, self.ollama)
                mail = Mail(sender=f"{name} <{address}>" if name and address else name or address,
                            received=c.received_at.astimezone(now.tzinfo), subject=item["title"],
                            preview=str(c.latest.get("bodyPreview") or ""),
                            focused={"focused": True, "other": False}.get(c.latest.get("inferenceClassification")))
                try:
                    judgment, comp = self.sorter.judge(mail, now.date(), model_key)
                except ModelUnavailable:
                    unavailable = True  # Ollama is down. Asking about the next message would only wait again.
                except ValueError:
                    pass  # an answer that couldn't be used. This message stays unsorted and is asked about next time.
                else:
                    usage.add(model_key, comp.input_tokens, comp.output_tokens, comp.latency_ms)
                    store.save_judgment(c.latest["id"], judgment.as_row(), model_key, stamp(now))
                    judged = judgment.as_row()
            if judged is None:
                unsorted += 1
                if c.latest.get("inferenceClassification") != "other":  # Outlook's own guess at what matters
                    items.append(Found(**item, owner="you", reason="Not sorted yet. The local model didn't answer."))
                continue
            if judged["action"] == "none":
                continue
            items.append(Found(**item, summary=judged["summary"] or None, category=judged["category"],
                               owner=judged["owner"], reason=judged["reason"] or None, due_at=judged["due_at"]))
        if unsorted:
            usage.note(f"{unsorted} {'message' if unsorted == 1 else 'messages'} couldn't be sorted, because the "
                       "local model didn't answer. The next build tries again.")
        return items
