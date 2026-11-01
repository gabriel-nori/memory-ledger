import { Button, Checkbox, Select, Icon } from "@/components";
import type { AgentConversation, VoiceStatus, VoiceVerbosity } from "@/api/types";
import { speech } from "./speech";
import type { VoiceSession } from "./useVoiceSession";
import { dictationProblem } from "./dictation";

const VERBOSITY = [
  { value: "", label: "Narration: global" },
  { value: "default", label: "Narration: default" },
  { value: "verbose", label: "Narration: verbose" },
  { value: "verbose_tts", label: "Narration: verbose with voice" },
];

/** What the voice is doing, in the words the person sees. */
export function stateLabel(s: VoiceSession): string {
  if (s.phase === "dictation") return "Listening (dictation)";
  if (s.phase === "listening") return "Listening";
  if (s.phase === "waiting") return s.speech.speaking ? "Speaking" : "Waiting for the agent";
  return "";
}

interface Props {
  session: VoiceSession;
  status: VoiceStatus | null;
  conv: AgentConversation | undefined;
  /** Speech is on for this chat (its own choice, else the global one). */
  ttsOn: boolean;
  writable: boolean;
  onTts: (on: boolean) => void;
  onVerbosity: (v: VoiceVerbosity | "") => void;
}

/** Why the microphone cannot be used right now, or "" when it can. */
function micProblem(status: VoiceStatus | null): string {
  return !status ? "" : status.stt.available ? dictationProblem() : status.stt.problem;
}

/** Dictation: the microphone inside the text box, on its right. Stop and Cancel replace it while recording. */
export function DictateButton({ session, status, writable }: Pick<Props, "session" | "status" | "writable">) {
  const busy = session.phase !== "idle";
  const problem = micProblem(status);
  const talking = session.speech.speaking || session.speech.pending > 0;
  if (session.phase === "dictation") {
    return (
      <span className="ml-agents__dictate">
        <Button size="sm" variant="primary" onClick={session.stop} aria-label="Stop dictation" title="Stop and keep the text">
          <Icon name="stop" /> Stop
        </Button>
        <Button size="sm" onClick={session.cancel} aria-label="Cancel dictation" title="Stop and discard what was dictated">
          <Icon name="close" /> Cancel
        </Button>
      </span>
    );
  }
  return (
    <span className="ml-agents__dictate">
      <Button
        size="sm"
        variant="ghost"
        aria-label="Dictate"
        title={problem || (talking ? "The voice is speaking; stop it first" : "Dictate with your voice")}
        disabled={!writable || !status || !!problem || busy || talking}
        onClick={session.startDictation}
      >
        <Icon name="mic" />
      </Button>
    </span>
  );
}

/** True when both speech directions work, so conversation mode makes sense. */
export function voiceReady(status: VoiceStatus | null): boolean {
  return !!status && status.stt.available && status.tts.available;
}

/** Conversation mode: the microphone that stands where Send does while nothing is typed. */
export function ConversationButton({ session, status, conv, writable }: Pick<Props, "session" | "status" | "conv" | "writable">) {
  const problem = micProblem(status);
  const on = session.conversation;
  return (
    <Button
      variant={on ? "primary" : "default"}
      aria-label="Conversation mode"
      aria-pressed={on}
      title={!conv ? "Send the first message to start a conversation by voice" : problem || (on ? "Stop the voice conversation" : "Listen, send after a pause, hear the answer, listen again")}
      disabled={!writable || !conv || !!problem || session.phase === "dictation"}
      onClick={() => session.setConversation(!on)}
    >
      <Icon name={on ? "stop" : "mic"} />
    </Button>
  );
}

/** Speech on or off for this chat, and how much the agent narrates. They live in the message options. */
export function VoiceOptions({ status, conv, ttsOn, writable, onTts, onVerbosity }: Omit<Props, "session">) {
  return (
    <>
      <span title={status && !status.tts.available ? status.tts.problem : "Read the agent's messages aloud, as they arrive"}>
        <Checkbox checked={ttsOn} onChange={onTts} label="Speak" disabled={!writable || !conv || !status?.tts.available} />
      </span>
      <Select
        aria-label="narration"
        title="How much the agent narrates its steps. A chat's own choice beats the global one in Settings."
        value={conv?.verbosity ?? ""}
        options={VERBOSITY}
        onChange={(e) => onVerbosity(e.target.value as VoiceVerbosity | "")}
        disabled={!writable || !conv}
      />
    </>
  );
}

/**
 * What the voice is doing, above the composer: the state, Stop speaking, and any
 * error. It shows only while there is something to say.
 */
export function VoiceBar({ session }: { session: VoiceSession }) {
  const talking = session.speech.speaking || session.speech.pending > 0;
  const label = stateLabel(session);
  if (!label && !talking && !session.speech.error) return null;
  return (
    <div className="ml-agents__voice" role="group" aria-label="voice controls">
      {talking ? (
        <Button size="sm" variant="primary" onClick={() => speech.interrupt()} aria-label="Stop speaking" title="Stop the voice and clear what is queued; the agent keeps working">
          <Icon name="stop" /> Stop speaking
        </Button>
      ) : null}
      <span className="ml-agents__voice-state" role="status" aria-live="polite">
        {label}
        {session.speech.error ? <span className="ml-agents__voice-err"> {session.speech.error}</span> : null}
      </span>
    </div>
  );
}
