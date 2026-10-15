# Capture anywhere - plan (executed natively; spec: docs/superpowers/specs/2026-10-14-capture-anywhere-design.md)

1. Settings: `custom_hotkeys` (list of {hotkey, action, delayed}), `save_folder`, `save_format`, `save_name`;
   validation + dedupe across all hotkeys; list support in from_dict/sanitized.
2. `uireport/output/export.py`: `save_dir(settings, output_root)`, `next_name(...)`, `save_image(img, settings, root)`,
   `copy_image(img)`, `drag_file(img, root, stem)`.
3. Controller: hotkey names `custom<i>`; generic side actions (chat/copy/save/copy_save) on the existing
   chat-capture path; editor copy/save/drag wiring.
4. Editor: Copy / Save as / Drag controls + Ctrl+Shift+C, Ctrl+S; filmstrip drag-out via injected provider.
5. Settings window: "More hotkeys" rows + "Saving" section.
6. Docs (README, CONTRACT, CLAUDE.md), real-run checks, restart app, merge, push.
