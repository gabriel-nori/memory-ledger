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

/**
 * The voice controls above the composer: dictate, Stop and Cancel while recording,
 * conversation mode, speech on or off for this chat, interrupt, and the state.
 */
export function VoiceBar({ session, status, conv, ttsOn, writable, onTts, onVerbosity }: Props) {
  const recording = session.phase === "dictation" || session.phase === "listening";
  const busy = session.phase !== "idle";
  const sttProblem = !status ? "" : status.stt.available ? dictationProblem() : status.stt.problem;
  const noMic = !writable || !status || !!sttProblem;
  const talking = session.speech.speaking || session.speech.pending > 0;
  const label = stateLabel(session);
  return (
    <div className="ml-agents__voice" role="group" aria-label="voice controls">
      {recording && session.phase === "dictation" ? (
        <>
          <Button size="sm" variant="primary" onClick={session.stop} aria-label="Stop dictation" title="Stop and keep the text">
            <Icon name="stop" /> Stop
          </Button>
          <Button size="sm" onClick={session.cancel} aria-label="Cancel dictation" title="Stop and discard what was dictated">
            <Icon name="close" /> Cancel
          </Button>
        </>
      ) : (
        <Button
          size="sm"
          aria-label="Dictate"
          title={sttProblem || (talking ? "The voice is speaking; stop it first" : "Dictate with your voice")}
          disabled={noMic || busy || talking}
          onClick={session.startDictation}
        >
          <Icon name="mic" /> Dictate
        </Button>
      )}
      <span title={!conv ? "Send the first message to start a conversation by voice" : sttProblem || "Listen, send after a pause, hear the answer, listen again"}>
        <Checkbox
          checked={session.conversation}
          onChange={session.setConversation}
          label="Conversation mode"
          disabled={noMic || !conv || session.phase === "dictation"}
        />
      </span>
      <span title={status && !status.tts.available ? status.tts.problem : "Read the agent's messages aloud, as they arrive"}>
        <Checkbox
          checked={ttsOn}
          onChange={onTts}
          label="Speak"
          disabled={!writable || !conv || !status?.tts.available}
        />
      </span>
      {talking ? (
        <Button size="sm" variant="primary" onClick={() => speech.interrupt()} aria-label="Stop speaking" title="Stop the voice and clear what is queued; the agent keeps working">
          <Icon name="stop" /> Stop speaking
        </Button>
      ) : null}
      <Select
        aria-label="narration"
        title="How much the agent narrates its steps. A chat's own choice beats the global one in Settings."
        value={conv?.verbosity ?? ""}
        options={VERBOSITY}
        onChange={(e) => onVerbosity(e.target.value as VoiceVerbosity | "")}
        disabled={!writable || !conv}
      />
      <span className="ml-agents__voice-state" role="status" aria-live="polite">
        {label}
        {session.speech.error ? <span className="ml-agents__voice-err"> {session.speech.error}</span> : null}
      </span>
    </div>
  );
}
