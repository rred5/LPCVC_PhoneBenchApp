# Two-stage tamper prompt (draft)

Draft for the LPCVC Track 3 comparison of runtimes (`llama_cpp` NPU vs `qairt`) and
of tampered-image outputs. Not wired into the runner yet; see "Runner notes".

- **Stage 1** decides real / tampered / fully_fake, names each edited object and the kind
  of edit, and explains why.
- **Stage 2** returns a bounding box for each edited object in absolute pixel coordinates.

Both prompts are under the server's 2000-character `max_prompt_chars`.

## Placeholders

| Placeholder | Filled with |
|---|---|
| `{width}`, `{height}` | Pixel size of the image actually sent to the phone (the prepared PNG, 768 px longest edge), not of the original. |
| `{tampered_objects}` | The `tampered_objects` list from the stage 1 JSON, as compact JSON. |

## Stage 1

```text
You are a forensic image analyst. Decide if this image is real, tampered, or fully_fake.

- real: an unedited camera photo.
- tampered: a real photo where one or more objects were edited locally: add (new object inserted), remove (object erased), replace (object swapped for another), occlude (object hidden or covered), or resize (object made larger or smaller).
- fully_fake: the whole image is AI-generated.

Check for: mismatched lighting or shadows, blurry or haloed edges, wrong perspective or scale, texture or noise that differs from its surroundings, bad reflections, and garbled text. For a removed object, name the area where something seems to be missing.

Reply with JSON only, in this order:
{"explanation": "<2-3 sentences: what looks inconsistent and why, or why nothing does>", "classification": "real" | "tampered" | "fully_fake", "tampered_objects": [{"object": "<name>", "edit": "add" | "remove" | "replace" | "occlude" | "resize", "location": "<where, in words>"}]}
Use [] for tampered_objects unless classification is "tampered".
```

## Stage 2

```text
This image is {width} pixels wide and {height} pixels tall. The origin (0, 0) is the top-left corner. x increases to the right and y increases downward.

These objects were identified as edited:
{tampered_objects}

For each one, give a tight bounding box in absolute pixel coordinates of this image: [x_min, y_min, x_max, y_max], with x between 0 and {width} and y between 0 and {height}. For a removed object, box the area where it used to be.

Reply with JSON only:
{"boxes": [{"object": "<name>", "bbox": [x_min, y_min, x_max, y_max]}]}
```

## Variant: "Explain why this image is tampered" (single stage)

For the known-tampered comparison (Nano Banana and Qwen-Inpaint images). This wording tells the
model the image is tampered, so it measures explanation and localization, not detection. If
"verdict accuracy" must still mean something here, use the two-stage prompt above instead.

```text
This image has been tampered with. Explain why this image is tampered: name each edited object, say how it was edited (add, remove, replace, occlude, or resize), and describe the visual clues that reveal it. Then give a tight bounding box for each edited object in absolute pixel coordinates [x_min, y_min, x_max, y_max]. The image is {width} pixels wide and {height} pixels tall; (0, 0) is the top-left corner.

Reply with JSON only:
{"explanation": "<2-3 sentences>", "tampered_objects": [{"object": "<name>", "edit": "add" | "remove" | "replace" | "occlude" | "resize", "bbox": [x_min, y_min, x_max, y_max]}]}
```

## Design choices

- **Explanation before verdict.** The 2026-09 pilot found the model naming the inserted object
  and still answering "real". Putting `explanation` first makes it write its observations
  before committing to a label. Worth an A/B against verdict-first; the decision metric is
  manipulation sensitivity without losing specificity on originals.
- **Edit vocabulary** (add, remove, replace, occlude, resize) matches the Track 3 tamper
  methods in the dataset filenames and the `GenAI_Edit_Suggestions` ground truth.
- **Explanation length.** Two to three sentences keeps decode time and thermal load down and
  is the right size for the LLM-as-judge explanation scoring from last year.
- **Bounding box, not polygon.** Small VLMs rarely produce reliable polygons and they cost
  more decode tokens. A `polygon` field can be added later.

## Runner notes (not implemented)

- **Context.** Stage 2 can continue the stage 1 conversation (reuses the image prefill, which
  dominates time to first token) or start fresh with `{tampered_objects}` filled in (pays the
  image prefill again and inflates stage 2 time). Pick one and keep it fixed across runtimes.
- **Gating.** Run stage 2 only when stage 1 returns a non-empty `tampered_objects`, or always
  run both. For the inference-time-vs-iteration (thermal) plot, always running both keeps the
  workload per iteration constant.
- **Token caps.** Suggested `max_tokens`: 256 for stage 1, 128 for stage 2.
- **Labels.** The existing harness scores `real` / `synthetic` / `tampered`. This prompt says
  `fully_fake`; map one to the other in the scorer.
- **Parsing.** Try strict JSON first, then fall back to the existing lenient parser
  (`lenient_classification` in `android/run_accuracy_pilot.py`). Report the strict-parse rate;
  it is itself a model comparison metric.
- **Ground-truth boxes.** Derive from the masks: `*_target_mask_prompt.png` (Nano Banana) and
  `*_mask.png` (Qwen-Inpaint), taking the bounding box of the non-zero pixels. Masks are not at
  the image resolution (for example 288x384 for a 768x1024 image), so compare boxes in
  coordinates normalized by each image's own width and height, and score IoU.

## Open risks

- **Coordinate convention differs by model.** Qwen2.5-VL grounds in absolute pixels of its own
  resized input (multiples of 28, so up to about 14 px off). Qwen3-VL is documented to use
  relative coordinates scaled 0-1000 regardless of the prompt. This is from memory of the Qwen
  docs and is untested here. Before comparing models, run a handful of masked images through
  each and check which scale the answers use; the scorer should convert per model.
- **Small-model JSON.** A 2B model may break the schema. The strict-parse rate will show it,
  and a one-line example in the prompt is the first thing to try if so.
