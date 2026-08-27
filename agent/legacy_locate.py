"""
Legacy-web field location — the adapter seam for surfaces whose form controls carry NO
accessible name: no ``<label for>``, no ``aria-label``, no ``placeholder``, no
``<th scope="row">``. MERIDIAN CORE is exactly this — labels are bare
``<td class="lbl">Amount:</td>`` cells next to ``<td><input name="amount"></td>``, and a live
``aria_snapshot()`` shows the input as an unnamed ``- textbox``. The take-home core's role+name
locators (recorder tiers 1-2, ``replay/engine.py::_locate``) resolve zero elements against
markup like that.

This module adds two resolution strategies, tried in this order, each anchored on something a
human or the server actually depends on rather than on styling:

  ``labeled_field`` — resolve a control by the visible label text sitting next to it. Anchors on
  what a human reads on screen; survives class/style churn; breaks only if the visible label
  copy itself changes.

  ``field_name`` — resolve by the control's ``name=`` attribute. On a server-rendered app the
  field name IS the form contract the backend requires, so it is highly stable — arguably more
  so than visible copy. It is explicitly NOT a test-id (an automation-only convenience legacy
  apps never have); it is load-bearing HTML the app cannot function without. Recorded as a weak
  tier only because it is opaque to a human reviewer.

Both ``recorder.py`` (building the artifact), ``replay/engine.py`` (deterministic replay) and
``agent/tools.py`` (live discovery execution) call into here, so the legacy-surface knowledge
lives in exactly one place.
"""
from __future__ import annotations

# Runs in the page/frame context. Returns, in document order, one entry per *unnamed* form
# control (no native <label>, aria-label, aria-labelledby, or title) — the controls the
# accessibility tree exposes with an empty name — with a best-effort label derived by proximity
# and the control's own name attribute. Order matches aria_snapshot()'s document order, so the
# caller can zip this against the tree's nameless control nodes.
DERIVE_UNNAMED_FIELD_LABELS_JS = r"""
() => {
  const norm = (s) => (s || "")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/^[*\s]+/, "")
      .replace(/[:\s]+$/, "")
      .trim();

  const controls = Array.from(document.querySelectorAll(
      'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea'
  ));

  const out = [];
  for (const el of controls) {
    const hasNativeName =
        (el.labels && el.labels.length) ||
        el.getAttribute('aria-label') ||
        el.getAttribute('aria-labelledby') ||
        el.getAttribute('title');
    if (hasNativeName) continue;  // the a11y tree already gives this one a usable name

    let label = "";
    // 1. a sibling label cell in the same table row (the MERIDIAN shape)
    const cell = el.closest('td, th');
    if (cell) {
      const row = cell.closest('tr');
      if (row) {
        const lblCell = row.querySelector('td.lbl, th.lbl, td[class~="lbl"], th[class~="lbl"]');
        if (lblCell && lblCell !== cell) label = lblCell.textContent;
      }
      // 2. a preceding cell's text
      if (!label && cell.previousElementSibling) label = cell.previousElementSibling.textContent;
    }
    // 3. a <label> element that isn't programmatically associated
    if (!label) {
      const wrapLabel = el.closest('label');
      if (wrapLabel) label = wrapLabel.textContent;
    }
    // 4. placeholder, then the name attribute as an opaque last resort
    if (!label) label = el.getAttribute('placeholder') || "";
    if (!label) label = el.getAttribute('name') || "";

    out.push({
      label: norm(label),
      name: el.getAttribute('name') || null,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || "").toLowerCase() || null,
    });
  }
  return out;
}
"""

CONTROL_ROLES = {
    "textbox", "combobox", "searchbox", "spinbutton",
    "checkbox", "radio", "slider", "listbox",
}


def normalize_label(text: str) -> str:
    """Match DERIVE_UNNAMED_FIELD_LABELS_JS's `norm`: collapse whitespace, drop a leading '*'
    and a trailing ':'. 'Operator ID:' -> 'Operator ID'."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed.lstrip("* ").rstrip(": ").strip()


def derive_unnamed_field_labels(ctx) -> list[dict]:
    """Run the derivation JS in a Playwright page or frame context. Never raises — returns []
    if the context can't be evaluated (mid-navigation, detached)."""
    try:
        return ctx.evaluate(DERIVE_UNNAMED_FIELD_LABELS_JS) or []
    except Exception:
        return []


def _xpath_label_predicate(norm_label: str) -> str:
    """XPath 1.0 predicate matching an element whose text, with ':' and '*' removed and
    whitespace normalized, equals `norm_label`. `translate(., ':*', '')` drops those chars;
    `normalize-space` then collapses whitespace. Labels on these legacy screens are plain
    alphanumeric text; a stray apostrophe is dropped rather than escaped (it would only ever
    weaken the match, never widen it to the wrong control)."""
    safe = norm_label.replace("'", "")
    return f"normalize-space(translate(., ':*', '')) = '{safe}'"


def _role_self_predicate(control_role: str | None) -> str:
    if control_role in ("combobox", "listbox"):
        return "self::select"
    if control_role in ("textbox", "searchbox", "spinbutton"):
        return "self::input or self::textarea"
    if control_role in ("checkbox", "radio"):
        return "self::input"
    return "self::input or self::select or self::textarea"


def locate_labeled_field(ctx, label: str, control_role: str | None = None):
    """
    Resolve a form control by the visible label text next to it. Returns a Playwright Locator
    (already narrowed to a single element) or None. Tries, in order: a label cell in the same
    table row; a non-associated ``<label>`` wrapper/sibling; any element with the matching text
    followed by a control. Prefers an exact single match; if a candidate matches more than one
    control it is skipped rather than guessed.
    """
    norm = normalize_label(label)
    if not norm:
        return None
    pred = _xpath_label_predicate(norm)
    self_pred = _role_self_predicate(control_role)

    candidates = [
        # label cell in the same row -> a control in a following sibling cell
        f"xpath=.//*[(self::td or self::th) and contains(concat(' ', normalize-space(@class), ' '), ' lbl ')]"
        f"[{pred}]/following-sibling::*[self::td or self::th]//*[{self_pred}]",
        # any label cell in the same row -> control anywhere later in that row
        f"xpath=.//tr[*[(self::td or self::th)[{pred}]]]//*[{self_pred}]",
        # a <label> element (not for=) -> the next control after it
        f"xpath=.//label[{pred}]/following::*[{self_pred}][1]",
        # loosest: any node with the text -> the next control in document order
        f"xpath=.//*[{pred}]/following::*[{self_pred}][1]",
    ]
    for selector in candidates:
        try:
            loc = ctx.locator(selector)
            count = loc.count()
        except Exception:
            continue
        if count == 1:
            return loc
        if count > 1:
            # ambiguous at this precision — try the next, more specific/looser candidate
            continue
    return None


def locate_field_name(ctx, name_attr: str | None):
    """Resolve a control by its ``name=`` attribute. Returns a single-element Locator or None."""
    if not name_attr:
        return None
    try:
        loc = ctx.locator(f'[name="{name_attr}"]')
        count = loc.count()
    except Exception:
        return None
    if count >= 1:
        return loc.first
    return None
