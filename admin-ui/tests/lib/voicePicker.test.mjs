import assert from "node:assert/strict";
import test from "node:test";
import { chosenVoice, previewBody } from "../../lib/voicePicker.ts";

const voices = [{ voice_id: "rachel", name: "Rachel" }, { voice_id: "adam", name: "Adam" }];

test("each Play asks for its own voice", () => {
  assert.deepEqual(previewBody("Adam", "adam"), { text: "Adam", voice_id: "adam" });
  assert.deepEqual(previewBody("hello"), { text: "hello" });
});

test("a listed saved voice shows by its list name", () => {
  assert.deepEqual(chosenVoice(voices, "adam", "ElevenLabs — Adam"), voices[1]);
});

test("a saved voice missing from the list still shows as chosen, under its saved label", () => {
  assert.deepEqual(chosenVoice(voices, "gone", "ElevenLabs — Old"), { voice_id: "gone", name: "ElevenLabs — Old" });
  assert.deepEqual(chosenVoice(voices, "gone"), { voice_id: "gone", name: "gone" });
});

test("nothing saved shows nothing chosen", () => {
  assert.equal(chosenVoice(voices, null, "x"), undefined);
});
