# Verify Worktree Before Editing
Run: pwd && git rev-parse --show-toplevel && git worktree list
Confirm with the user which path is the intended edit target. If a worktree is active, all edits must go there, not the main repo path.
