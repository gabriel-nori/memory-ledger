"""Request bodies for the write routes.

These are what the generated OpenAPI document shows, so each field says what it
means. They are deliberately tolerant in the ways the page and the tests rely on:

  * a missing body is an empty one — every route then refuses on its merits
    ("missing id"), which is the reason worth showing a person;
  * `null` for a text field means empty, as it always did;
  * a junk entry inside a list of objects is skipped rather than failing the
    whole call (the page builds these lists from free-form UI state).

A field of the wrong TYPE is refused (400), never stored as its repr. Length
caps and the closed sets are enforced in the handlers, not here: the error
strings they produce are part of the contract.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, create_model

import graph


def _none_to_empty(v):
    return "" if v is None else v


def _dict_or_none(v):
    return v if isinstance(v, dict) else None


def _str_or_none(v):
    return v if isinstance(v, str) else None


#: Text where `null` means "". Absent and empty are the same thing for these.
Text = Annotated[str, BeforeValidator(_none_to_empty)]
#: A list entry that is skipped, not refused, when it is not an object / string.
Loose = Annotated[Optional[Any], BeforeValidator(_dict_or_none)]
LooseStr = Annotated[Optional[str], BeforeValidator(_str_or_none)]


def _loose(model):
    return Annotated[Optional[model], BeforeValidator(_dict_or_none)]


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


def T(description: str = "", default: str = ""):
    return Field(default, description=description or None)


# ------------------------------------------------------------------ tasks
class Link(Body):
    id: Text = T("The node to link to.")
    rel: Text = T("One of the relations `/api/meta` lists.")


class SetDue(Body):
    id: Text = T("The node id.")
    due: Text = T("ISO timestamp. Empty clears it.")


class AddTask(Body):
    title: Text = T("Required.")
    type: Text = T("`task` (default) or `subtask`.")
    body: Text = ""
    due: Text = T("ISO date or timestamp.")
    realm: Text = T("Defaults to the configured realm.")
    assignee: Text = ""
    priority: Text = ""
    links: list[_loose(Link)] = Field(
        default_factory=list,
        description="Relations to create from the new task. Up to 20; one with an "
                    "unknown relation is reported under `rejected`, not applied.")


class ProposeIdea(Body):
    title: Text = T("Required.")
    detail: Text = T("The alternative in full: what it is and why it came up.")
    realm: Text = T("Lowercase letters, digits, dashes. Default: the configured one.")


class Complete(Body):
    id: Text = T("The node id.")
    note: Text = T("Kept on the node as the completion note.")


class SetStatus(Body):
    id: Text = T("The node id.")
    status: Text = T("One of the statuses `/api/meta` lists.")
    waiting_on: Optional[str] = Field(
        None, description="Who or what it waits on. Omit to leave it as it is.")


class SetAssignee(Body):
    id: Text = T("The node id.")
    assignee: Text = T("Empty un-assigns.")


class UpdateNode(Body):
    """Every field but `id` is optional; one that is sent is written, in one write."""
    id: Text = T("The node id.")
    title: Optional[str] = Field(None, description="Cannot be blanked.")
    body: Optional[str] = None
    priority: Optional[str] = Field(None, description="P1–P4, or empty for none.")
    due: Optional[str] = Field(None, description="YYYY-MM-DD or YYYY-MM-DD HH:MM; "
                                                 "empty clears it.")
    realm: Optional[str] = Field(None, description="Empty keeps the current realm.")


# ----------------------------------------------------------------- people
class Assign(Body):
    id: Text = T("The task id.")
    person_id: Text = T("A person node. Empty un-assigns and drops the edge.")


class MergePerson(Body):
    canonical: Text = T("The person node that stays.")
    duplicate: Text = T("The person node folded into it.")


class AddAlias(Body):
    id: Text = T("The person node.")
    alias: Text = T("Another spelling they answer to.")


# ------------------------------------------------------------------ graph
class Edge(Body):
    src: Text = ""
    dst: Text = ""
    rel: Text = T("One of the relations `/api/meta` lists.")


# ------------------------------------------------------------- settings
class Settings(Body):
    """Only the keys sent are written. An empty string REMOVES a key, falling
    back to the environment variable or the built-in default."""
    tz: Optional[str] = Field(None, description="IANA timezone name.")
    realm: Optional[str] = Field(None, description="Lowercase letters, digits, dashes.")
    owner: Optional[str] = Field(None, description="Who 'me' is.")
    brain_name: Optional[str] = Field(None, description="What the brain is called: `{brain}` "
                                                         "in templates and wake phrases.")
    backend: Optional[str] = Field(None, description="sqlite | neo4j | auto.")
    autosync: Any = Field(None, description="true | false; null or empty resets it.")
    consolidate: Any = Field(None, description="true | false; null or empty resets it.")
    ui_sync: Any = Field(None, description="true | false; null or empty resets it.")
    learned_rules: Any = Field(None, description="true | false; the learned-rules detector, "
                                                  "OFF by default. null or empty resets it.")
    prompt_recall: Any = Field(None, description="true | false; gated per-prompt recall, "
                                                  "OFF by default. null or empty resets it.")
    agents_sync: Any = Field(None, description="true | false; mirror the Agents tab chats to "
                                                "the master, OFF by default. null or empty resets it.")
    map_poll_seconds: Any = Field(None, description="1–600.")
    dash_refresh_seconds: Any = Field(None, description="5–3600.")


class Neo4jConfig(Body):
    url: Text = T("The neo4j HTTP endpoint, e.g. http://host:7474. Empty removes "
                  "the master.")
    db: Text = T("Default `neo4j`.")
    user: Text = T("Default `neo4j`.")
    password: Text = T("Blank keeps the stored one.")


class ProposalDecision(Body):
    id: Text = T("The rule proposal.")
    title: Text = T("Accept only: replaces the proposed title. Empty keeps it.")
    text: Text = T("Accept only: replaces the proposed rule text. Empty keeps it.")
    reason: Text = T("Reject only: why, kept on the proposal.")


class Empty(Body):
    pass


class Maintain(Body):
    confirm: Any = Field(None, description="Must be true. Without it the call returns "
                                           "ok:false and needs_confirmation:true.")
    keep_days: Any = Field(2, description="Leave the last N days of captures loose.")


class Capture(Body):
    enabled: Any = Field(None, description="true records, false stops recording.")


class UiPrefs(Body):
    prefs: Annotated[Optional[dict[str, Any]], BeforeValidator(_dict_or_none)] = Field(
        None, description="The whole blob. Replaces what is stored.")


class RenameRealm(Body):
    old: Text = T("The realm to rename.")
    new: Text = T("Its new name; an existing realm is merged into.")


class Backup(Body):
    label: Text = T("Default `manual`.")


# ---------------------------------------------------------------- trigger
class TriggerGate(Body):
    scope: Text = T("`slack:<channel>`, `slack:<channel>:<thread_ts>`, `jira:<ISSUE-KEY>` "
                    "or `github:<owner>/<repo>#<number>`.")
    enabled: Any = Field(None, description="true opens the conversation to other people's "
                                           "mentions, false closes it again.")


_obj = Annotated[Optional[dict[str, Any]], BeforeValidator(_dict_or_none)]


class TriggerConfig(Body):
    """A patch: only the keys sent are changed. Secrets are write-only."""
    enabled: Any = Field(None, description="The master switch for every trigger.")
    signature: Any = Field(None, description="The line every reply ends with; `{owner}` "
                                             "is the owner's name. Also the loop guard.")
    refusals: _obj = Field(None, description="`not_enabled`: the notice for a mention in a "
                                             "conversation nobody opened. `policy`: the one "
                                             "for a request that breaks a rule; `{reason}` "
                                             "is the one-line why. `not_allowed`: the one for "
                                             "a blocked user or an owner-only command.")
    translate_notices: Any = Field(None, description="Send notices in the language of the "
                                                     "message they answer (a model call).")
    claude: _obj = Field(None, description="`bin`, `mcp_config`, `strict_mcp_config`, "
                                           "`timeout_s`, `max_concurrent`, `extra_args`, `cwd`.")
    refuse_without_model: Any = Field(None, description="A refusal is posted as written "
                                                        "through the source's API, or only "
                                                        "logged: never a model call.")
    slack: _obj = Field(None, description="`enabled`, `owner_user_id`, "
                                          "`thread_lookback_days`, `dms`, `dms_open`, "
                                          "`allowed_users`, "
                                          "`blocked_users`, `events` (`{kind: {on, do, action, "
                                          "http, script}}`), `commands` (the whole list), "
                                          "`prompt` (the old mention run command), and the "
                                          "write-only `app_token`, `bot_token`, `user_token`.")
    jira: _obj = Field(None, description="`enabled`, `base_url`, `email`, "
                                         "`owner_account_id`, `marker`, `poll_seconds`, `jql`, "
                                         "`watch_jql`, `max_catchup_hours`, `events`, `prompt`, "
                                         "and the write-only `token`.")
    meeting: _obj = Field(None, description="`enabled`, and `phrases` (the whole list: "
                                            "`{phrase, on, anyone, description, do, action, "
                                            "http, script}`). A phrase may use `{brain}` and "
                                            "`{owner}`.")
    github: _obj = Field(None, description="`enabled`, `api_url`, `login`, `marker`, "
                                           "`poll_seconds`, `repos`, `trusted_associations`, "
                                           "`refusal_notice`, `max_catchup_hours`, `events` "
                                           "(`{kind: {on, do}}`, `do` being that event's run "
                                           "command), and the write-only `token`. An unknown "
                                           "`{variable}` in a run command is refused.")
    clear: Optional[list[LooseStr]] = Field(None, description="Secrets to remove, e.g. "
                                                              "`[\"slack.user_token\"]`.")


class TriggerChannel(Body):
    channel: Text = T("A channel id (C0123ABCD) or a link to it.")
    name: Text = T("How to show it. Looked up from Slack when empty and the bot can.")
    enabled: Any = Field(None, description="true opens the channel, false closes it again "
                                           "(the row is kept, marked removed).")


class TriggerPerson(Body):
    source: Text = T("Whose list: `jira` (the default).")
    user_id: Text = T("Their id on that source: a Jira account id.")
    name: Text = T("How to show them. Optional; a name given replaces the stored one.")
    enabled: Any = Field(None, description="true lets them tag the brain anywhere on the source, "
                                           "false takes them off again (the row is kept).")


class TriggerDecline(Body):
    request_id: Text = T("The request the session will not do.")
    reason: Text = T("One short line, safe to show the requester.")
    rule: Text = T("The rule or guardrail it breaks.")


class TriggerScan(Body):
    text: Text = T("The meeting's transcript or notes. A line `Name: what they said` "
                   "names who said it.")
    where: Text = T("The meeting, in words. Default `Meeting: <title>`.")
    title: Text = T("The meeting's title.")
    link: Text = T("A link to its notes or transcript.")
    at: Text = T("When it happened, ISO 8601.")
    key: Text = T("What makes this meeting itself, so a second scan runs nothing twice. "
                  "Default: the link, else the title.")
    me: Optional[list[LooseStr]] = Field(None, description="Other names the transcript "
                                                           "calls the owner.")
    dry_run: Any = Field(None, description="true: say what matches; log and run nothing.")


class TriggerReprocess(Body):
    request_id: Text = T("The blocked or failed request to run again.")


class TriggerCancel(Body):
    request_id: Text = T("The queued or running request to stop.")


class TriggerCheck(Body):
    only: Text = T("`claude`, `slack`, `jira` or `github`; empty checks everything switched on.")
    offline: Any = Field(False, description="true skips the calls to Slack and Jira.")


class CustomTrigger(Body):
    id: Text = T("The trigger's id, which is also its file name in `<home>/triggers`.")
    text: Text = T("The whole YAML, as written.")
    check_only: Any = Field(False, description="true validates and writes nothing.")


class CustomTriggerId(Body):
    id: Text = T("The trigger's id.")


class CustomTriggerEnable(Body):
    id: Text = T("The trigger's id.")
    enabled: Any = Field(None, description="true or false: rewrites only `enabled:` in its file.")


class TriggerAction(Body):
    request_id: Text = T("The request the action was taken for.")
    kind: Text = T("What was done: `reply`, `jira-transition`, `slack-message`, ...")
    target: Text = T("Where: a permalink, an issue key, a channel.")
    detail: Text = T("What, in a sentence a person could audit.")


# -------------------------------------------------------------------- b2b
class B2BConfig(Body):
    """A patch of `<home>/b2b.json`: only the keys sent change. The private key
    is write-only."""
    enabled: Any = Field(None, description="Poll the brokers (needs the trigger master "
                                           "switch on, an identity and an enabled broker).")
    brokers: Optional[list[Any]] = Field(None, description="The whole broker list, "
                                         "`[{id?, name, url, enabled}]`; replaces the stored one.")
    private_key: Any = Field(None, description="An X25519 private key (base64url) to use as "
                                               "this brain's identity. Write-only.")
    poll_seconds: Any = Field(None, description="How often to read the brokers, 30 to 86400.")
    do: Any = Field(None, description="The run command for a received message; "
                                      "`{variables}` as for triggers, plus `b2b_*`.")
    action: Any = Field(None, description="`claude`, `http` or `script`.")
    http: _obj = Field(None, description="`method`, `url`, `headers`, `body`.")
    script: _obj = Field(None, description="`name`, `args`.")
    clear: Optional[list[LooseStr]] = Field(None, description="`[\"private_key\"]` removes it.")


class B2BIdentity(Body):
    force: Any = Field(False, description="Replace the identity already stored. Messages "
                                          "waiting for the old id can no longer be opened.")


class B2BBroker(Body):
    id: Text = T("The broker to change; empty adds a new one.")
    url: Text = T("Its base URL, http(s).")
    name: Text = T("How to call it.")
    enabled: Any = Field(None, description="Poll it, and send through it.")
    remove: Any = Field(False, description="true drops the broker.")


class B2BBrokerCheck(Body):
    id: Text = T("The broker to log in to now.")


class B2BPeer(Body):
    peer_id: Text = T("The other brain's user id: their X25519 public key.")
    name: Text = T("How to call them.")
    broker_id: Text = T("The broker that reaches them; may be empty when there is only one.")
    person_id: Text = T("The person node they are: a node id, or a name that resolves to one.")
    enabled: Any = Field(None, description="true connects the peer, false disconnects it "
                                           "(the row is kept, marked removed).")
    allow: Any = Field(True, description="With enabled, also put the peer on the allowed "
                                         "list (default true); false leaves the list as is.")


class B2BAllowed(Body):
    user_id: Text = T("The other brain's user id: their X25519 public key.")
    name: Text = T("How to call them.")
    enabled: Any = Field(None, description="true lets them send to this brain, false blocks "
                                           "them (the row is kept, marked removed).")


class B2BSend(Body):
    to: Text = T("A connected peer's user id, name, or person.")
    text: Text = T("The message.")
    human: Any = Field(False, description="A person typed it (the Messages chat). Sealed "
                                          "with the text, so the peer sees it; anything "
                                          "else is shown as automated.")


# ------------------------------------------------------------------ agents
class AgentAttach(Body):
    name: Text = T("The file's name, as the person sees it.")
    data: Text = T("The file's bytes, base64. At most 25 MB.")


class AgentSend(Body):
    text: Text = T("The person's message. May be empty when `attachments` is not.")
    attachments: list[LooseStr] = Field(
        default_factory=list,
        description="Paths `/api/agent-attach` returned, up to 10; each is added to the "
                    "message as a link the agent can read from disk.")
    conversation: Text = T("The conversation to add it to; empty starts one, titled "
                           "from the message.")
    connector: Text = T("The connector to answer with; empty keeps the conversation's.")
    model: Text = T("The model; empty is the connector's default.")
    full_tools: Any = Field(None, description="Tools on (`true`) or read-only (`false`) "
                                              "from this turn on; omitted keeps it.")
    context_turns: Any = Field(None, description="Earlier exchanges each turn carries, "
                                                 "0 to 10; omitted keeps it.")


class AgentConversation(Body):
    """A patch of one conversation, or a new empty one when `id` is empty."""
    id: Text = T("The conversation; empty creates one.")
    title: Any = Field(None, description="Its name in the list.")
    connector: Any = Field(None, description="The connector its next turn uses.")
    model: Any = Field(None, description="The model its next turn uses.")
    full_tools: Any = Field(None, description="Tools on, or read-only.")
    context_turns: Any = Field(None, description="Earlier exchanges each turn carries, 0 to 10.")
    hidden: Any = Field(None, description="Hidden from the active list.")
    verbosity: Any = Field(None, description="`default`, `verbose` or `verbose_tts`; empty follows the global setting.")
    tts: Any = Field(None, description="`on` or `off` for this chat's speech; empty follows the global setting.")
    draft: Any = Field(None, description="The unsent message in the composer.")
    draft_at: Any = Field(None, description="When the draft was edited (ISO); an older one than the stored draft is ignored.")


class VoiceConfig(Body):
    """A patch of the voice settings; only the keys sent change."""
    stt_language: Any = Field(None, description="Spoken language expected: pt, en, es or it.")
    stt_auto: Any = Field(None, description="Let Whisper detect the language each session.")
    tts_language: Any = Field(None, description="Default language of the voice.")
    tts_auto: Any = Field(None, description="Detect each block's language to pick the voice.")
    translate: Any = Field(None, description="Translate every block into `translate_target`.")
    translate_target: Any = Field(None, description="The language blocks are translated into.")
    tts_enabled: Any = Field(None, description="Speak the agent's blocks.")
    tts_background: Any = Field(None, description="Also speak chats that are not on screen.")
    silence_s: Any = Field(None, description="Seconds of silence that send the message in conversation mode, 2 to 15.")
    tts_voices: Any = Field(None, description="Voice per language, e.g. {\"pt\": \"Luciana\"}.")
    verbosity: Any = Field(None, description="`default`, `verbose` or `verbose_tts`.")


class VoiceSpeak(Body):
    text: Text = T("The block to read aloud; markdown is cleaned first.")
    language: Text = T("pt, en, es or it; empty detects it from the text.")
    voice: Text = T("A voice id from `/api/voice`; empty picks the configured one.")


class VoiceTranslate(Body):
    text: Text = T("The block to translate.")
    target: Text = T("pt, en, es or it; empty uses the configured target.")


class AgentRetry(Body):
    id: Text = T("A reply that failed.")


class AgentCancel(Body):
    id: Text = T("A reply that is queued or running.")


class AgentPlugin(Body):
    key: Text = T("The plugin, as `name@marketplace`.")
    enabled: Any = Field(None, description="`true` turns it on, anything else off.")


class AgentConfig(Body):
    """A patch of `<home>/agents.json`: only the keys sent change."""
    connectors: Optional[list[Any]] = Field(None, description="The whole connector list, "
                                            "`[{id?, name, kind, enabled, ...}]`; replaces "
                                            "the stored one.")
    default_connector: Any = Field(None, description="The connector a new conversation uses.")
    context_turns: Any = Field(None, description="The default for a new conversation, 0 to 10.")
    recall_limit: Any = Field(None, description="How many recalled nodes a turn gets, 0 to 30.")
    recall_budget: Any = Field(None, description="Their total size cap, in characters.")
    max_concurrent: Any = Field(None, description="Replies running at once, 1 to 8.")


# -------------------------------------------------------------- contracts
_sections = Annotated[Optional[dict[str, Any]], BeforeValidator(_dict_or_none)]


class CreateRule(Body):
    title: Text = T("Required.")
    source: Text = T("Default `user`.")
    section: Text = ""
    sections: _sections = Field(
        None, description="Contract sections by field; `the_rule` and "
                          "`judged_against` are required. See `/api/meta`.")
    scenario: Text = T("When the rule applies, in the words a matcher reads.")
    non_negotiable: Any = False
    overrides: Text = ""
    realm: Text = ""
    id: Text = T("Refresh this rule rather than create one.")


_decl = Annotated[Optional[list[Any] | str], Field(
    None, description="A list of names, or one comma-separated string.")]

CreateSkill = create_model(
    "CreateSkill",
    __base__=Body,
    __doc__="A skill and, in `new_rules`, the rules it is given inline.",
    title=(Text, T("Required.")),
    name=(Text, T("Defaults to the title.")),
    description=(Text, ""),
    triggers=(Text, ""),
    realm=(Text, ""),
    id=(Text, T("Refresh this skill rather than create one.")),
    sections=(_sections, Field(None, description="The §5.1 sections by field. "
                                                 "See `/api/meta`.")),
    rules=(list[LooseStr], Field(default_factory=list,
                                 description="Ids of rules it honors.")),
    new_rules=(list[_loose(CreateRule)], Field(
        default_factory=list, description="Rules created FIRST, in this call.")),
    **{field: _decl for field, _label in graph.DECL_LABELS},
)


class CreateProcess(Body):
    title: Text = ""
    body: Text = ""
    trigger: Text = ""
    realm: Text = T("Lowercase letters, digits, dashes. Default: the configured one.")
    id: Text = T("Refresh this process rather than create one.")


class ExportSkills(Body):
    kind: Text = T("`learned` (default) or `plugin`.")
    dir: Text = T("Where to write. Default: <home>/skills-export.")


class IngestSkills(Body):
    dir: Text = ""


class ExportBundle(Body):
    skills: list[LooseStr] = Field(default_factory=list)
    rules: list[LooseStr] = Field(default_factory=list)
    processes: list[LooseStr] = Field(default_factory=list)


class UploadBundle(Body):
    filename: Text = ""
    data: Text = T("The zip, base64 (a data: URL is accepted).")


class ImportBundle(Body):
    token: Text = T("From upload-bundle.")
    select: Annotated[Optional[dict[str, Any]], BeforeValidator(_dict_or_none)] = Field(
        None, description="Item id to update | replace | ignore.")
    process_realm: Text = T("Realm the bundle's processes land in; default the "
                            "configured realm.")


class ExportScripts(Body):
    names: list[LooseStr] = Field(default_factory=list,
                                  description="Script filenames; empty exports them all.")


class UploadScripts(Body):
    filename: Text = T("The file's name. A name not ending in .zip is staged as one "
                       "script by that name.")
    data: Text = T("A scripts zip or one script file, base64 (a data: URL is accepted).")


class CreateScript(Body):
    name: Text = T("A bare filename, e.g. `deploy.sh`.")
    text: Text = T("The script itself.")
    executable: Any = Field(True, description="Write it with the executable bit (default).")
    replace: Any = Field(False, description="Overwrite one by that name, after a backup.")


class ImportScripts(Body):
    token: Text = T("From upload-scripts.")
    select: Annotated[Optional[dict[str, Any]], BeforeValidator(_dict_or_none)] = Field(
        None, description="Script name to replace | keep. A name left out is not imported.")


class DeleteContracts(Body):
    ids: list[LooseStr] = Field(default_factory=list)
    force: Any = Field(False, description="Delete built-ins too.")
