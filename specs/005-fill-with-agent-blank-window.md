# 005: Fill with agent opened a blank browser window

Status: done, in review.

## Problem

Pressing Fill with agent on the dashboard opened a browser showing only `about:blank`, and
the agent never filled anything.

## How Fill with agent works

1. The dashboard's button sends `POST /jobs/<id>/fill` to the local dashboard server.
2. The server picks the command: a supported board (Greenhouse, Lever, Ashby,
   SmartRecruiters) uses its hand-off filler; any other company form
   uses the form agent (`python -m agent.form_agent --job-url ...`).
3. It checks the Anthropic API answers, then starts the command in its own console window.
4. The form agent opens a visible Chrome window on the project's own browser profile
   (`.playwright/profile`), so your sign-ins on job sites are kept between runs.
5. It loads your profile and resume, opens the application page, and the agent fills it.
   You sign in where asked, review, and submit.

## Causes

The console window closes as soon as the program ends, so its error was never seen. Code
and a local test point to these causes; the exact error on the candidate's computer was not
available.

1. Likely the main cause. Chrome allows one browser per profile. When an earlier Fill with
   agent or hand-off window is still open, a second launch on the same profile hands its
   blank start page to the open browser and exits. That is a new `about:blank` tab in the
   old window (the screenshot shows other tabs beside it). The launch then fails, the
   program ends, and its console window closes.
2. The window showed nothing while the agent loaded the profile and resume and opened the
   page. If the page never loaded (a site that blocks automated browsers, or no network),
   the error ended the program and closed the window with no explanation.
3. When Chrome restored earlier tabs, the agent could work in a tab other than the one in
   front.
4. Reported after the first fix: the console showed the agent working, but no Chrome window
   appeared. Windows does not let a program started in the background (the dashboard)
   put its window in front, so the agent's Chrome opened behind the dashboard's browser.
   Chrome also reopens a window where it last was, which can be off screen after a monitor
   change.
5. Reported after the second fix (Oct 8, 2026): the window shows, but while the agent fills
   the form you cannot scroll to the bottom of the page. Playwright draws every page at a
   fixed 1280x720 unless told otherwise, whatever the window's size. In a smaller window
   the bottom of the form is cut off, and scrolling cannot reach it.

## Fix

- The dashboard checks whether the agent's browser profile is in use before starting
  anything. If it is, it says: "The agent's browser window is still open from an earlier
  Fill with agent or hand-off. Finish or close that window, then try again." The hand-off
  and form agent commands check too.
- The window shows "Opening <company> - <role>" right away, and the agent works in that
  tab, brought to the front.
- If the application page does not open, the window says why and gives the link, and the
  console says the same.
- Any error now stays on screen until you press Enter.
- Once open, the agent's window is minimized and restored, which brings it in front on
  Windows, and moved to the top-left corner of the main screen. The console also says to
  click Chrome in the taskbar if the window is still not visible.
- The hand-off and form agent browser opens with `no_viewport=True`, so the page fits the
  window and can be scrolled to the bottom, and it resizes with the window.

## How to test

Offline tests in `tests/test_fill_with_agent.py`: profile lock detection (Windows
`lockfile`, macOS and Linux `SingletonLock`, and a real Chromium profile when installed), the
dashboard answering 409 instead of launching, hand-off refusing a second browser, the tab
choice, the message when the page does not open, and errors waiting for Enter.
`tests/test_review.py` checks the hand-off browser opens with `no_viewport=True`, and
`tests/test_fill_with_agent.py` shows, with a real Chromium when installed, that a fixed
viewport is 1280x720 in an 800x500 window while `no_viewport` fits the window.
