# Argus Download Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Argus content discovery and PDF download resilient to DOM changes, iframe embedding, and alternate delivery paths.

**Architecture:** Keep the existing scrape flow, but add a surface-agnostic discovery layer that scans the main page plus nested frames, and a download orchestration layer that accepts browser downloads, popup tabs, or network PDF responses. Reuse the existing HTTP/article fallbacks after these stronger in-browser strategies are exhausted.

**Tech Stack:** Python, Playwright sync API, unittest

---

### Task 1: Add failing tests for resilience helpers

**Files:**
- Create: `tests/test_download_hardening.py`
- Test: `tests/test_download_hardening.py`

- [ ] **Step 1: Write the failing test**

```python
def test_collect_candidates_reads_iframe_surfaces():
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python3 -m unittest tests/test_download_hardening.py`
Expected: FAIL because frame-aware helper does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def iter_document_surfaces(page):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python3 -m unittest tests/test_download_hardening.py`
Expected: PASS

### Task 2: Harden in-browser discovery and PDF capture

**Files:**
- Modify: `scripts/hnxcl.py`
- Test: `tests/test_download_hardening.py`

- [ ] **Step 1: Write the failing test**

```python
def test_download_capture_accepts_pdf_network_response():
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `rtk python3 -m unittest tests/test_download_hardening.py`
Expected: FAIL because only expect_download path exists.

- [ ] **Step 3: Write minimal implementation**

```python
def capture_pdf_artifact(...):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `rtk python3 -m unittest tests/test_download_hardening.py`
Expected: PASS

### Task 3: Regression verification

**Files:**
- Modify: `scripts/hnxcl.py`
- Test: `tests/test_verified_text_fallback.py`
- Test: `tests/test_download_hardening.py`

- [ ] **Step 1: Run targeted tests**

Run: `rtk python3 -m unittest tests/test_download_hardening.py tests/test_verified_text_fallback.py`
Expected: PASS

- [ ] **Step 2: Run suite**

Run: `rtk python3 -m unittest discover -s tests`
Expected: PASS
