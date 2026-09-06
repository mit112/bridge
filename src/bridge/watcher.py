"""Poll the transcripts dir for changes and fire a debounced callback.

Stdlib-only (no watchfiles). The naive version stat()ed every *.jsonl under
`root` on every 0.5s poll; against the real corpus (9,202 files) that is ~18k
stat calls a second and measured 11.9% CPU over 21h of an idle panel. The work
now scales with the *change*, not the corpus:

  * **Directories.** Creating or deleting a file bumps its parent directory's
    mtime, so one stat per directory (hundreds, not thousands) finds every new
    or removed transcript, and only the directories whose mtime moved are
    re-listed. New subdirectories are walked in when their parent is re-listed.
  * **The hot set.** Appending to an existing file does NOT bump the parent
    directory, so files whose mtime is within `hot_s` are stat()ed every poll.
    That is the set an active session can be writing to, and it is tiny; a
    transcript nobody has touched in five minutes cannot start growing without
    something else in the tree moving first.
  * **Directories are hot too.** A filesystem with coarse (1s) directory mtimes
    could otherwise hide a file created in the same tick as the baseline stat,
    so a recently-touched directory is re-listed unconditionally -- but only
    for `dir_hot_s`, which is a couple of polls and not the five minutes the
    file hot set gets. That guard only has to outlive one mtime tick, and a
    re-list is `scandir` + one `stat` per entry: the real corpus has one
    directory holding 74% of its 11,392 transcripts, and re-listing it costs
    22.6 ms. At the old window, any activity in it meant paying that on every
    0.5s poll for the next five minutes.
  * **The full walk.** Every `full_s` (15s, the reindex cadence) the whole tree
    is re-walked and reconciled, so anything the cheap passes cannot see -- a
    cold file mutated in place, a directory whose mtime was restored -- is
    still picked up, just not within the second.

  * **Scope.** `watch_dir` prunes directories the caller does not care about.
    Watching a file the reindex will never open is pure cost twice over: the
    stat itself, and -- far worse -- the full reindex its change fires. See
    `indexer.indexed_dirs` for the production predicate and what it measured.

`on_change` fires only when the *.jsonl file set or one of its (mtime, size)
pairs actually changed; a directory mtime moving on its own is not a change.
On a detected change the watcher waits for a `quiet_s` lull before firing
`on_change` once, coalescing a burst of writes into one reindex.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# (mtime, size) per *.jsonl path; mtime per directory.
Files = dict[str, tuple[float, int]]
Dirs = dict[str, float]


class FileWatcher:
    def __init__(
        self, root: Path, on_change: Callable[[], None],
        poll_s: float = 0.5, quiet_s: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        hot_s: float = 300.0, full_s: float = 15.0,
        dir_hot_s: float = 2.0,
        watch_dir: Callable[[str], bool] | None = None,
    ) -> None:
        self._root = Path(root)
        self._on_change = on_change
        self._poll_s = poll_s
        self._quiet_s = quiet_s
        self._clock = clock
        self._hot_s = hot_s
        self._dir_hot_s = dir_hot_s
        # Which directories are worth watching at all. Default: everything, so
        # a caller that has no opinion still gets the whole subtree.
        self._watch_dir = watch_dir or (lambda _dirpath: True)
        self._full_s = full_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # The baseline is taken HERE, not on the thread. `start()` returning has
        # to mean "everything from now on counts as a change": the thread can be
        # descheduled between `Thread.start()` and its first statement, and any
        # write that lands in that gap would otherwise be absorbed into the
        # baseline and never fire. On a loaded machine that silently drops the
        # first transcript written after `bridge serve` boots.
        baseline = self._walk()
        self._thread = threading.Thread(target=self._run, args=baseline, daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _walk(self) -> tuple[Dirs, Files]:
        """The full tree: every directory's mtime and every *.jsonl's stat."""
        dirs: Dirs = {}
        files: Files = {}
        try:
            for dirpath, subdirs, names in os.walk(self._root):
                subdirs[:] = [
                    d for d in subdirs if self._watch_dir(os.path.join(dirpath, d))
                ]
                try:
                    dirs[dirpath] = os.stat(dirpath).st_mtime
                except OSError:
                    continue
                for name in names:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    files[path] = (st.st_mtime, st.st_size)
        except OSError as exc:
            # An unreadable root is the normal state before the first Claude
            # session is ever recorded; the next walk picks it up.
            log.debug("transcript walk of %s stopped early: %s", self._root, exc)
        return dirs, files

    def _relist(self, dirpath: str, dirs: Dirs, files: Files) -> bool:
        """Re-read ONE directory (not its subtree). True if its files changed."""
        try:
            entries = list(os.scandir(dirpath))
        except OSError:
            return self._forget(dirpath, dirs, files)

        changed = False
        seen: set[str] = set()
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.path not in dirs and self._watch_dir(entry.path):
                        # A directory created since the last walk: adopt its
                        # whole subtree, which may already hold transcripts.
                        changed |= self._adopt(entry.path, dirs, files)
                    continue
                if not entry.name.endswith(".jsonl"):
                    continue
                st = entry.stat()
            except OSError:
                continue
            seen.add(entry.path)
            current = (st.st_mtime, st.st_size)
            if files.get(entry.path) != current:
                files[entry.path] = current
                changed = True

        for path in [p for p in files if os.path.dirname(p) == dirpath]:
            if path not in seen:
                del files[path]
                changed = True
        return changed

    def _adopt(self, dirpath: str, dirs: Dirs, files: Files) -> bool:
        """Walk a newly-appeared subtree in. True if it contributed a file."""
        changed = False
        try:
            for sub, subdirs, names in os.walk(dirpath):
                subdirs[:] = [
                    d for d in subdirs if self._watch_dir(os.path.join(sub, d))
                ]
                try:
                    dirs[sub] = os.stat(sub).st_mtime
                except OSError:
                    continue
                for name in names:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(sub, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    current = (st.st_mtime, st.st_size)
                    if files.get(path) != current:
                        files[path] = current
                        changed = True
        except OSError as exc:
            log.debug("could not adopt new directory %s: %s", dirpath, exc)
        return changed

    def _forget(self, dirpath: str, dirs: Dirs, files: Files) -> bool:
        """Drop a vanished directory and everything under it."""
        prefix = dirpath + os.sep
        for known in [d for d in dirs if d == dirpath or d.startswith(prefix)]:
            del dirs[known]
        changed = False
        for path in [p for p in files if p.startswith(prefix)]:
            del files[path]
            changed = True
        return changed

    def _poll(self, dirs: Dirs, files: Files) -> bool:
        """One cheap pass. Mutates `dirs`/`files`; True if the files changed."""
        now = time.time()
        changed = False
        for dirpath in list(dirs):
            if dirpath not in dirs:
                continue  # already dropped with a vanished parent
            try:
                mtime = os.stat(dirpath).st_mtime
            except OSError:
                changed |= self._forget(dirpath, dirs, files)
                continue
            if mtime != dirs[dirpath] or now - mtime <= self._dir_hot_s:
                dirs[dirpath] = mtime
                changed |= self._relist(dirpath, dirs, files)

        for path, previous in list(files.items()):
            if now - previous[0] > self._hot_s:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue  # the directory pass owns deletions
            current = (st.st_mtime, st.st_size)
            if current != previous:
                files[path] = current
                changed = True
        return changed

    def _reconcile(self, dirs: Dirs, files: Files) -> bool:
        """The periodic full walk: authoritative, and the only pass that can
        see a change neither a directory mtime nor the hot set exposes."""
        fresh_dirs, fresh_files = self._walk()
        changed = fresh_files != files
        dirs.clear()
        dirs.update(fresh_dirs)
        files.clear()
        files.update(fresh_files)
        return changed

    def _run(self, dirs: Dirs, files: Files) -> None:
        pending_since: float | None = None
        last_full = self._clock()
        while not self._stop.wait(self._poll_s):
            if self._clock() - last_full >= self._full_s:
                last_full = self._clock()
                changed = self._reconcile(dirs, files)
            else:
                changed = self._poll(dirs, files)
            if changed:
                pending_since = self._clock()          # (re)start the quiet window
                continue
            if pending_since is not None and self._clock() - pending_since >= self._quiet_s:
                pending_since = None
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001 - a bad reindex must not kill the watcher
                    log.exception("file watcher on_change failed")
