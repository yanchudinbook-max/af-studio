#!/usr/bin/env python3
"""
Build the deployable site from a Claude Design export.

    python3 build.py [src/bundle.html]

Claude Design exports a single self-contained HTML file with every asset
base64-inlined. That means the browser downloads ~1.8 MB before it can paint
anything. This script pulls the JPEGs out into separate WebP files, rewrites
the references, and injects the meta tags the export doesn't carry.

Fonts and JS stay inline — they're small, and inlining them avoids extra
round trips.

Re-run this after every new export from Claude Design.
"""
import base64
import json
import pathlib
import re
import subprocess
import sys

SRC = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "src/bundle.html")
OUT = pathlib.Path("index.html")
IMGDIR = pathlib.Path("img")

SITE = "https://af-studio.art/"
TITLE = "Arina Franchuk — AI Production"
DESC = (
    "Создание визуального контента полного цикла для брендов, инфлюенсеров, "
    "музыкантов, дизайнеров и частных проектов — от идеи и визуальной концепции "
    "до готового изображения или видео."
)
OG_IMAGE = SITE + "og.jpg"
OG_ALT = "Портретная AI-визуализация: девушка с тигром в кафельном интерьере"

# Stable, readable filenames. Claude Design keeps asset UUIDs stable between
# exports, so these survive a re-export; anything unlisted falls back to its
# UUID prefix.
NAMES = {
    "d210e5b6": "hero-tiger",
    "708c2751": "dried-flowers",
    "25112b44": "anastasya-portrait",
    "53aec3e1": "red-dress-figures",
    "5ef16dae": "anastasya-red-dress",
}

WEBP_QUALITY = "82"

# The bundled Archivo webfont is subset to Latin + Cyrillic, so U+2197 (↗) has
# no glyph in any font in the stack and falls through to a system symbol font.
# That fallback renders thin and ignores the surrounding font-weight: 800, so
# the arrow looks unrelated to the label beside it. Forcing a font-family does
# not help — measured identical glyph widths across the whole stack. Draw it
# instead: currentColor and a stroke we control match the text on any device.
ARROW_SVG = (
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"'
    ' style="width:.72em;height:.72em;display:inline-block;'
    'vertical-align:-.01em;margin-left:.26em">'
    '<path d="M3.4 12.6 12.6 3.4M5.9 3.4h6.7v6.7" fill="none"'
    ' stroke="currentColor" stroke-width="2.6" stroke-linecap="square"/></svg>'
)

# Mobile corrections layered over the export. Both problems are in the design
# source; fix them in Claude Design when convenient and these become no-ops.
MOBILE_CSS = """
@media (max-width: 600px) {
  /* The image grid goes full-bleed via an INLINE style
     (width:100vw; margin-left:calc(50% - 50vw)), cancelling main's 20px
     gutter. Its figcaptions inherit that, so caption text sat hard against the
     screen edge while every other line started at 20px. Drop the bleed on
     phones so images and captions share the page gutter — !important is
     required to beat the inline style. */
  main > section:has(> figure) {
    width: auto !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    /* The grid's own minmax(170px) no longer fits twice once 40px of gutter is
       taken back, which would silently collapse it to one column. Lower the
       floor to keep the designed two-up layout. */
    grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr)) !important;
  }

  /* Touch targets: every link was 12-27px tall against the 44px minimum.
     An invisible overlay expands the hit area without moving any layout. */
  a { position: relative; }
  a::after {
    content: "";
    position: absolute;
    left: -6px;
    right: -6px;
    top: 50%;
    transform: translateY(-50%);
    height: max(100%, 44px);
  }

  /* Exception: the artist page's streaming list is a flex column with only
     6px between rows, so 44px overlays would overlap each other and cause
     mis-taps. Those rows get real height instead. */
  nav a { padding-block: 10px; }
  nav a::after { content: none; }
}
@media (max-width: 480px) {
  /* Header is 1fr auto 1fr: the middle label IS centred on the viewport, but
     "ARINA FRANCHUK" nearly fills the left column while "CONTACT" leaves its
     column half empty, so the gaps read 22px / 72px and the label looks glued
     to the brand. Hide the label at this width — the H1 immediately below
     repeats it verbatim. visibility (not display) keeps the grid column, so
     the brand stays left and contact stays right. */
  header span { visibility: hidden; }
}
"""


def json_for_html(obj):
    """Serialise so the result is safe to embed inside a <script> tag."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\u002F")


def meta_block(indent="  "):
    tags = [
        f'<meta name="description" content="{DESC}">',
        f'<link rel="canonical" href="{SITE}">',
        '<meta name="theme-color" content="#F5F5F3">',
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml">',
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">',
        "",
        '<meta property="og:type" content="website">',
        f'<meta property="og:site_name" content="{TITLE}">',
        f'<meta property="og:url" content="{SITE}">',
        f'<meta property="og:title" content="{TITLE}">',
        f'<meta property="og:description" content="{DESC}">',
        f'<meta property="og:image" content="{OG_IMAGE}">',
        '<meta property="og:image:type" content="image/jpeg">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        f'<meta property="og:image:alt" content="{OG_ALT}">',
        '<meta property="og:locale" content="ru_RU">',
        "",
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{TITLE}">',
        f'<meta name="twitter:description" content="{DESC}">',
        f'<meta name="twitter:image" content="{OG_IMAGE}">',
        f'<meta name="twitter:image:alt" content="{OG_ALT}">',
    ]
    return "".join(f"{indent}{t}\n" if t else "\n" for t in tags)


def main():
    if not SRC.exists():
        sys.exit(f"missing export: {SRC}")
    html = SRC.read_text(encoding="utf-8")

    man_m = re.search(
        r'(<script type="__bundler/manifest"[^>]*>)(.*?)(</script>)', html, re.S
    )
    tpl_m = re.search(
        r'(<script type="__bundler/template"[^>]*>)(.*?)(</script>)', html, re.S
    )
    if not man_m or not tpl_m:
        sys.exit("export does not look like a Claude Design bundle")

    manifest = json.loads(man_m.group(2))
    template = json.loads(tpl_m.group(2))

    IMGDIR.mkdir(exist_ok=True)
    extracted, saved_before, saved_after = [], 0, 0

    for uuid, entry in list(manifest.items()):
        if not entry["mime"].startswith("image/"):
            continue
        if entry.get("compressed"):
            sys.exit(f"asset {uuid} is compressed; unpacking not implemented")

        raw = base64.b64decode(entry["data"])
        name = NAMES.get(uuid[:8], uuid[:8])
        src_ext = entry["mime"].split("/")[-1].replace("jpeg", "jpg")
        tmp = IMGDIR / f"{name}.{src_ext}"
        webp = IMGDIR / f"{name}.webp"
        tmp.write_bytes(raw)

        subprocess.run(
            ["cwebp", "-quiet", "-q", WEBP_QUALITY, str(tmp), "-o", str(webp)],
            check=True,
        )
        tmp.unlink()

        # The unpacker substitutes only UUIDs still present in the manifest
        # (template.split(uuid).join(blobUrl)), so a reference must be
        # rewritten here before its manifest entry is dropped.
        template = template.replace(uuid, f"img/{webp.name}")
        del manifest[uuid]

        saved_before += len(raw)
        saved_after += webp.stat().st_size
        extracted.append((webp.name, len(raw), webp.stat().st_size))

    # Drop the space too — an inline SVG separated by a space can wrap onto its
    # own line, leaving an orphaned arrow under the label.
    arrows = template.count(" ↗")
    template = template.replace(" ↗", ARROW_SVG)
    if template.count("↗"):
        print(f"  warning: {template.count(chr(0x2197))} arrow(s) not in the"
              " expected ' ↗' form, left as text")

    # Meta tags also go into the template head: the runtime replaces the whole
    # document, so a JS-executing crawler sees the template's head, not ours.
    # The mobile CSS must land here too — the shell's <style> is discarded with
    # the rest of the shell document when the app mounts.
    template = template.replace(
        '<meta charset="utf-8">',
        '<meta charset="utf-8">\n'
        + meta_block("")
        + "<style>"
        + MOBILE_CSS
        + "</style>\n",
        1,
    )

    out = html[: man_m.start(2)] + json_for_html(manifest) + html[man_m.end(2):]
    tpl_m = re.search(
        r'(<script type="__bundler/template"[^>]*>)(.*?)(</script>)', out, re.S
    )
    out = out[: tpl_m.start(2)] + json_for_html(template) + out[tpl_m.end(2):]

    out = out.replace("<html>", '<html lang="ru">', 1)
    out = out.replace(
        f"  <title>{TITLE}</title>\n", f"  <title>{TITLE}</title>\n" + meta_block(), 1
    )

    OUT.write_text(out, encoding="utf-8")

    print(f"  arrows redrawn as SVG: {arrows}")
    for name, before, after in extracted:
        print(f"  {name:28} {before/1024:7.0f} KB -> {after/1024:6.0f} KB")
    print(f"\nimages: {saved_before/1024/1024:.2f} MB -> {saved_after/1024/1024:.2f} MB")
    print(f"html:   {len(html)/1024/1024:.2f} MB -> {len(out)/1024:.0f} KB")


if __name__ == "__main__":
    main()
