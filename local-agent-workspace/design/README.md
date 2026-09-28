# Local workspace design

Original reference: `concept.png`, generated with the built-in Image Gen tool.
The September 16 user-supplied Claude Code screenshots supersede its composer
layout, sidebar density, typography and palette. The original remains as design history.

## Current Code layout

- Near-white canvas `#fcfcfb`, light sidebar `#f7f7f5`, terracotta accent retained.
- Compact 270 px sidebar (320 px above 1600 px), 54 px top bar, 924 px content width.
- DM Sans for heading, controls and body; 25 px empty-state heading and 15 px messages.
- Bottom composer with a 50 px minimum input and 15 px radius. Project/Local chips
  sit above the input. Attachment, permission mode and real model selection sit below.
- Permission menu opens upward, with five working modes, a selected checkmark and
  descriptions that reflect this application's behavior.
- Mobile uses a sidebar drawer and a fluid composer/menu.
- Original Local branding and functional project suggestions remain. Hosted-service
  navigation, account names, fake usage statistics and unsupported controls from
  the screenshots are not copied into this local application.

Original brief: A complete Claude-inspired local coding assistant, original Local wordmark,
warm ivory canvas, beige sidebar, editorial serif heading, spacious white composer,
terracotta actions, fine outlined controls. Full prompt is saved in `prompt.txt`.

## Original concept tokens

- Canvas `#faf9f6`; sidebar `#eeede7`; surface `#ffffff`.
- Text `#292923`; muted `#827f74`; border `#d8d5cd`; accent `#b66b50`.
- Compact desktop: sidebar 244 px, header 56 px, composer maximum 740 px.
- At the reference's 1505 × 1045 size: sidebar 288 px, header 64 px, composer
  864 px, 60 px heading and 24 px subtitle. These measurements correct the
  initial token estimates against the generated image's actual proportions.
- Georgia serif for wordmark and empty-state heading; system sans for controls.
- Heading: 42 px desktop / 32 px mobile; body: 15 px; controls: 13 px.
- Composer: 18 px radius; buttons: 9 px; understated borders and shadows.
- Thin line icons, 18 px, consistent 1.7 stroke; four-diamond CSS brand motif.
- Mobile: sidebar drawer, fluid composer, full-width workspace drawer.

## Screen inventory

Sidebar: Local / workspace; New conversation; selected project; Conversations;
empty session hint; Settings; real gateway connection status.

Primary screen: New conversation; Workspace; What shall we work on?; Your files,
tools, and ideas. In one place.; composer; live model selector; Explore this
project / Make a change / Run a command; Runs on your machine · Models through
Databricks.

Conversation state extends the same design: user bubbles, Markdown replies,
expandable tool cards, approval actions, pinned composer and stop control.
Workspace drawer contains actual files, a text editor, Git changes, and a command
runner. Settings dialog configures workspace/model/runtime and the external
credential file. No static conversation data or simulated gateway responses.
