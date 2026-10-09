# Build your own avatar

Aion can wear any character that VMagicMirror can load. This guide walks from "I have an idea of what she or he should look like" to a working `.vrm` file on your desktop. Pick the route that matches your time and skill; the first route takes an afternoon, the last can take weeks.

## What VMagicMirror needs

VMagicMirror loads VRM files (VRM 0.x and 1.0). For Aion to work well, the model needs:

| Part | Why |
| --- | --- |
| Humanoid bone rig (hips, spine, head, arms, hands, legs) | VMagicMirror poses the body and head from these bones. |
| Mouth shapes A, I, U, E, O (named `aa`, `ih`, `ou`, `ee`, `oh` in VRM 1.0) | Lip sync opens the mouth with these. Without them the mouth stays shut while she talks. |
| Blink | Idle blinking. |
| Spring bones on hair and loose clothing (optional) | Hair and fabric sway when the head moves. |

A VRM exported from VRoid Studio has all four. A mesh from an image-to-3D generator has none of them and needs the work described in Route B.

## Step 1: Decide what the character looks like

Start with pictures. You need a front view at minimum, and a side and back view help a lot.

- **Draw it.** A sketch on paper, photographed, is enough to guide the later steps.
- **Use a photo.** A photo of yourself works for a likeness. Use only your own face or a person who has agreed to it.
- **Generate it.** Image models such as ChatGPT's image generation, Higgsfield, or Midjourney can turn a description or a sketch into a character sheet. Ask for a "full-body character turnaround, front, side, and back views, A-pose, arms slightly away from the body, plain white background". The A-pose and plain background make modeling and rigging easier later.

Avoid recreating a known character from a game, film, or brand. Besides the legal problem, VRM files carry a license field, and a model of someone else's character cannot be licensed honestly.

## Step 2: Turn the pictures into a VRM

### Route A: VRoid Studio (easiest, anime style)

[VRoid Studio](https://vroid.com/en/studio) is free from Pixiv and runs on Windows and macOS. You shape the face, hair, body, and clothing with sliders and brushes, then export a VRM file. Keep your reference images open beside it and match proportions, hair, and outfit by eye.

- Export with **File > Export as VRM**. Fill in the author and license fields when prompted.
- The export includes the rig, mouth shapes, blink, and spring bones, so it works in VMagicMirror immediately.
- Textures can be painted in VRoid itself or exported, edited in any image editor, and imported again. This is how you add details your references show.

### Route B: image-to-3D, then rig in Blender (more realistic, much more work)

Image-to-3D services (for example Higgsfield's 3D generation, Meshy, Tripo, or Hunyuan3D) turn one or more pictures into a textured 3D mesh, usually a `.glb` file. The result is a statue: it has no bones, no mouth shapes, and often fused fingers and hair. To make it a VRM:

1. Import the `.glb` into [Blender](https://www.blender.org/) (free).
2. Clean the mesh: separate hair and clothing if needed, fix the hands, reduce the polygon count if VMagicMirror runs slowly.
3. Add a humanoid skeleton and skin the mesh to it. Free options include Blender's Rigify, [Mixamo](https://www.mixamo.com/) auto-rigging, and Reallusion AccuRIG.
4. Sculpt shape keys (blendshapes) for A, I, U, E, O and a blink.
5. Install the [VRM Add-on for Blender](https://extensions.blender.org/add-ons/vrm/) (by saturday06), assign the humanoid bones and expressions, add spring bones to the hair, and export VRM.

Expect several rounds of fixing. Generators get the overall look quickly and the details (hands, eyes, the inside of the mouth) poorly. Dr. Finley's photorealistic version of Aion took many iterations of texture and mesh edits.

### Route C: start from an existing model

- Download a base model with a license that allows editing (VRoid Hub and BOOTH list the license on each model page), then customize it in VRoid Studio or Blender.
- Commission an artist. Many VRoid and VRM artists take commissions on sites such as VGen and BOOTH. Send your reference sheet and say you need a VRM with A/I/U/E/O, blink, and spring bones for VMagicMirror.

Aion's original model was a VRoid-based VRM customized by a friend who builds VRMs, and her later look started from a CC BY base model. Check and keep the credit that each license requires.

## Step 3: Tweak the model later

- **VRoid Studio** for anything made there: reopen the `.vrm` project, change hair or clothes, export again.
- **Blender with the VRM add-on** for mesh and texture changes on any VRM.
- **Unity with [UniVRM](https://github.com/vrm-c/UniVRM)** for material and shader changes.
- **An AI assistant driving Blender.** With a Blender MCP add-on (for example [blender-mcp](https://github.com/ahujasid/blender-mcp)), an assistant such as Claude can run Blender scripts for you: recolor clothing, swap textures, adjust a shape key, render a preview from a fixed camera so you can compare versions. Keep a copy of every version you approve, and ask for one change at a time so you can tell what moved.

## Step 4: Put the avatar on your desktop

1. Open VMagicMirror and load the `.vrm`.
2. Turn on the transparent background so the window shows only the character, then drag her to a corner of the screen and resize.
3. Set the lip sync microphone to **CABLE Output (VB-Audio Virtual Cable)** (see the main README).
4. Decide on face tracking. VMagicMirror can follow your face with a webcam, or animate the head and hands from your keyboard and mouse with no camera. Either works with Aion.

The [VMagicMirror manual](https://malaybaku.github.io/VMagicMirror/en/) documents each setting for the current version.

## Other apps that show a VRM avatar

VMagicMirror is the app Aion is set up for, because it shows a transparent desktop character driven by keyboard and mouse. Other VRM apps can work with the same audio routing (VB-CABLE into the app's lip sync input), with different strengths:

| App | Notes |
| --- | --- |
| [VSeeFace](https://www.vseeface.icu/) | Free, Windows. Face tracking and audio lip sync. Aimed at streaming (OBS capture) more than a desktop overlay. |
| [Warudo](https://store.steampowered.com/app/2079120/Warudo/) | Free on Steam. Node-based scripting and many integrations; heavier to set up. |
| [VTube Studio](https://denchisoft.com/) | Uses Live2D (2D) models, so it needs a separate 2D avatar pipeline. |

Whichever app you choose, check that it can (1) load your model, (2) take lip sync from a selectable audio input, and (3) show the character on a transparent or chroma-keyed background.
