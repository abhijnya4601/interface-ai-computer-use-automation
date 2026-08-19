"""
Fixtures for offline (no-browser) perception tests.

These are not invented — they are the literal `page.locator("html").aria_snapshot()` /
`frame.locator("html").aria_snapshot()` output captured from a real Chromium run against the
real Flask app during development (see DECISIONS.md D6). Using real captured output rather than
hand-guessed shapes means these tests would have caught the two real surprises this build hit
(the removed `page.accessibility` API and the iframe boundary) had they existed before that
investigation, instead of after.
"""

# (a) the search page before any query is submitted
SEARCH_PAGE_ARIA_YAML = """\
- document:
  - heading "Member Search" [level=1]
  - table:
    - rowgroup:
      - row "Search (ID / name) Go":
        - cell "Search (ID / name)"
        - cell "Go":
          - textbox "Search (ID / name)"
          - button "Go"
"""

# (b) search results with 2+ rows — real captured output for query "12345", trimmed to the
# results table (the full page also wraps everything in an outer layout table, reproduced here
# so the nested-table-layout stress case is represented).
SEARCH_RESULTS_ARIA_YAML = """\
- document:
  - table:
    - rowgroup:
      - row "Member Search Search (ID / name) 12345 Go Member ID Name Actions 12345 Whitfield, Dana View":
        - cell "Member Search Search (ID / name) 12345 Go Member ID Name Actions 12345 Whitfield, Dana View":
          - heading "Member Search" [level=1]
          - table:
            - rowgroup:
              - row "Search (ID / name) 12345 Go":
                - cell "Search (ID / name)"
                - cell "12345 Go":
                  - textbox "Search (ID / name)": "12345"
                  - button "Go"
          - table:
            - rowgroup:
              - row "Member ID Name Actions":
                - columnheader "Member ID"
                - columnheader "Name"
                - columnheader "Actions"
            - rowgroup:
              - row "12345 Whitfield, Dana View":
                - cell "12345"
                - cell "Whitfield, Dana"
                - cell "View":
                  - link "View":
                    - /url: /member/12345
              - row "23456 Oyelaran, Marcus View":
                - cell "23456"
                - cell "Oyelaran, Marcus"
                - cell "View":
                  - link "View":
                    - /url: /member/23456
"""

# (c) the confirm-frame content — real captured output from `frame.locator("html")
# .aria_snapshot()` on the sub-account confirmation iframe's own document. This is the fixture
# that proves per-frame snapshotting reaches content the top-level snapshot does not — the
# top-level snapshot of the wrapper page around this iframe is CONFIRM_WRAPPER_TOP_LEVEL_YAML
# below, which stops at a bare `iframe` leaf.
CONFIRM_FRAME_ARIA_YAML = """\
- document:
  - table:
    - rowgroup:
      - row "Member Dana Whitfield (12345)":
        - rowheader "Member"
        - cell "Dana Whitfield (12345)"
      - row "Account Type christmas_club":
        - rowheader "Account Type"
        - cell "christmas_club"
      - row "Nickname Holiday":
        - rowheader "Nickname"
        - cell "Holiday"
      - row "Opening Deposit $25.00":
        - rowheader "Opening Deposit"
        - cell "$25.00"
  - button "Confirm and Open Account"
"""

# The top-level wrapper page around the iframe above — real captured output, confirms the
# iframe boundary: no "Confirm and Open Account" text reaches this snapshot at all.
CONFIRM_WRAPPER_TOP_LEVEL_YAML = """\
- document:
  - heading "Review and Confirm" [level=1]
  - paragraph: Please review the details below before opening this sub-account.
  - iframe
"""


# Pre-parsed dict trees (post-_parse_aria_snapshot shape) for exercising prune_accessibility_tree
# in isolation, including cases (deep nesting, decorative wrappers) that are easier to construct
# directly than to encode as YAML text.

DEEPLY_NESTED_TREE = {
    "role": "document",
    "children": [
        {
            "role": "generic",
            "children": [
                {
                    "role": "generic",
                    "children": [
                        {
                            "role": "generic",
                            "children": [
                                {"role": "button", "name": "buried button"},
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}

TREE_WITH_DECORATIVE_WRAPPERS = {
    "role": "document",
    "children": [
        {"role": "generic", "children": []},  # empty decorative wrapper -> should be dropped
        {"role": "presentation"},  # decorative, no name/value/children -> dropped
        {"role": "button", "name": "Go"},  # meaningful -> kept
        {"role": "generic", "name": "labelled wrapper"},  # generic but has a name -> kept
    ],
}
