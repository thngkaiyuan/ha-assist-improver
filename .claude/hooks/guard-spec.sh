#!/bin/bash
# Registered in .claude/settings.json, so every checkout and worktree runs it. SPEC.md changes come by pull request,
# which the maintainer approves; the git hooks (when installed, as in his checkout) refuse a commit that changes it
# unless KAI_OK_SPEC=1 (tools/git-hooks/spec-guard), and a push to the public repository that holds a household term
# (tools/git-hooks/leak-guard). This asks the person before a command that sets it or skips those hooks, so no agent
# approves its own. Edits themselves don't ask: the commit is where a change is caught. Only setting it counts (export,
# env, or a prefix assignment that starts a command), not text that mentions it.
cmd=$(jq -r '.tool_input.command // ""')
sets='(^|[;&|(])[[:space:]]*((export|env)[[:space:]]+([^[:space:]]+[[:space:]]+)*)?KAI_OK_SPEC='

ask() {
  jq -n --arg r "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "ask",
    permissionDecisionReason: $r}}'
  exit 0
}

grep -Eq "$sets" <<<"$cmd" && ask "This commits a change to SPEC.md, which the maintainer approves."
grep -Eq 'git[^|;&]*commit[^|;&]*(--no-verify|[[:space:]]-[a-zA-Z]*n[[:space:]])' <<<"$cmd" \
  && ask "This commit skips the SPEC.md check."
grep -Eq 'git[^|;&]*push[^|;&]*--no-verify' <<<"$cmd" && ask "This push skips the leak guard."
exit 0
