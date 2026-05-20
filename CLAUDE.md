# GymLaunch Intelligence — Claude Instructions

## Before writing any Lambda function

Always ask the user what to name the Lambda function before writing any code.
Do not invent or reuse a name without explicit confirmation.

## File write location

Always write files directly to `/mnt/data/gymlaunch_intelligence/` (the main repo path),
not to any `.claude/worktrees/` path. The user interacts with files at the main path
and should not have to commit a worktree to see changes.
