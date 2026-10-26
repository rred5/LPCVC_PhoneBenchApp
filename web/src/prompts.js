// A prompt is text, or a list of turns asked in order in one conversation
// (the image goes with the first). This is the shape the server takes in
// run.prompts; a prompt with one turn is always plain text.

export const DEFAULT_MAX_TURNS = 4;

export function turnsOf(prompt) {
  return Array.isArray(prompt) ? prompt : [prompt];
}

export function isMultiTurn(prompt) {
  return Array.isArray(prompt) && prompt.length > 1;
}

// Trimmed, blank turns dropped; null when nothing is left.
export function tidyPrompt(prompt) {
  const turns = turnsOf(prompt)
    .filter((turn) => typeof turn === "string")
    .map((turn) => turn.trim())
    .filter(Boolean);
  if (!turns.length) return null;
  return turns.length === 1 ? turns[0] : turns;
}

export function tidyPrompts(prompts) {
  return prompts.map(tidyPrompt).filter((prompt) => prompt !== null);
}

// A prompt whose turns are all text (a stored or submitted shape check).
export function isPromptShape(prompt, maxText, maxTurns = 10) {
  const ok = (turn) => typeof turn === "string" && turn.length <= maxText;
  return ok(prompt) || (Array.isArray(prompt) && prompt.length >= 1 && prompt.length <= maxTurns && prompt.every(ok));
}
