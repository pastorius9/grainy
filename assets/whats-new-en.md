# Grainy: what's new

Short English notes shown in the app after an update. The full change log is CHANGELOG.md (Korean).

## 0.5.98 — 82 film looks

- **A Film panel in Develop.** Choose from 82 films — Kodak Portra, Ektar, Gold, Kodachrome, Ektachrome, Tri-X, T-MAX and Vision3, Fujifilm Velvia, Provia, Astia, Superia, Pro 400H and Acros, Ilford HP5, FP4 and Delta, Fomapan, Agfa and more.
- Each look is computed from its maker's published technical data (spectral sensitivity, characteristic curves, dye densities): negatives are printed on the paper they were made for, slides are viewed as they are. Film amount blends it with your photo, and the film's grain goes into Effects › Grain.
- AI programs can choose a film by name in apply_edits (film_look).

## 0.5.97 — Let an AI program work in Grainy (MCP)

- **AI programs such as Claude Code, Codex, Gemini CLI and Claude Desktop can use Grainy's own tools.** Register Grainy's MCP once with one command, then just ask, e.g. "convert this roll as one roll of negatives, run Auto colour and export it". The commands are under Settings › AI Connection (MCP)… and on pastorius9.github.io/grainy.
- Edits show in Grainy at once and the last five AI tasks can be undone. Nothing to switch on in Grainy; only AI programs you register on this computer can connect, while Grainy is open. Grainy itself sends nothing over the internet; what the AI program reads is sent to that AI service.
- Batch edits from the Photo menu keep each RAW photo's own colour settings.

## 0.5.96 — Photos no longer go offline after updating on macOS

- **macOS: photos no longer go offline after every update.** macOS remembers per app which folders and disks it may read. Each macOS build of Grainy used to carry a different signature, so macOS took every update for a new app and forgot the permission. All releases are now signed with the same certificate. The first update to this version may ask once more; later ones keep it.
- **A photo the system will not let Grainy read is shown as needing access, not as offline**, with its saved preview, and Grainy explains once how to allow the folder again (on macOS with a button that opens the right System Settings page).
- Reported through the Feedback button (#UMJ92). Thank you.

## 0.5.95 — Feedback reference numbers

- **Sending feedback now gives you a reference number**, such as `#K7Q2M`: five random characters, nothing about you or your computer.
- **When your feedback is addressed, Grainy tells you after the update** that ships it, once, at the top of this window. No e-mail or account is involved: the reference stays on your computer and the list of addressed references ships with the app.

## 0.5.94 — Turned radial gradients, photo background, overlay that steps aside

- **Radial gradients can be turned and can start off the photo.** A `Turn ellipse` slider appears with a radial gradient selected; start dragging beside the photo or past its edge so only part of the light falls in the frame. Linear gradients can reach past the edge too.
- **The background behind the photo can be black, dark grey, mid grey or white**, from View › Photo background or a right-click on the photo.
- **The red mask overlay steps aside while you move the mask's sliders** and comes back when you paint again or press `O`.
- **`[` and `]` resize the brush wherever the keyboard focus is.**
- **The tone curve's region sliders sit right under the curve.**
- Thank you for the suggestions sent through the Feedback button.

## 0.5.93 — A brush that acts on chosen tones only

- **Brush masks have a `Tonal range`.** With the brush selected in the Masks tab, the tool options offer `All` · `Shadows` · `Midtones` · `Highlights`. Paint after choosing one and the adjustment reaches only those tones of what you painted: for dodging the shadows or burning the highlights.
- Changing it on a mask you already painted applies at once; `All` takes the limit away. The limit appears in the mask list, where its range and edge softness can be refined.
- Suggested through the Feedback button. Thank you.

## 0.5.92 — What's new after an update, spin box arrows

- **After an update, Grainy shows what changed the first time it opens.** The notes come with the app; nothing is downloaded. They can be opened again from Settings › `What's new in this version…`.
- **The up arrow of number boxes works.** In the export window (`Quality`, `Longest edge`) and the dust detection windows, the number field covered most of the up arrow on Windows 11, so clicking it did nothing. The two arrows are now stacked on the right edge. Thank you for the report.
