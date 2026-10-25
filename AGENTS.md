# Agent instructions

These instructions apply to the entire repository.

## Repository language

English is the repository language. Write all new or changed repository text in English.
This includes code comments, docstrings, documentation, application messages, commit messages, issues, and pull requests.

Use technical English in accordance with [ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/).
Use the writing rules and the approved dictionary meanings. The points below are a summary, not a replacement for the specification.

- Use short, clear sentences. Give one instruction in each step.
- Use the imperative form for instructions.
- Use the same technical name for the same item throughout the text.
- Use approved words with their approved meanings and parts of speech.
- Use necessary technical names and technical verbs as permitted by ASD-STE100.
- Use American English spelling. Do not use contractions, idioms, or slang.
- Keep code identifiers, API names, instrument commands, and protocol values exact. Do not translate or rewrite these literals.

## Issue workflow

Use a separate branch and a separate Git worktree for each issue.
Do not make issue changes in the main checkout or on the `main` branch.
Always open a pull request with `main` as the base branch.

1. Read the issue and its acceptance criteria. Fetch the current `origin/main` before you start a new branch.
2. Create a dedicated branch from `origin/main` in a separate worktree.
   Use the branch format `<type>/issue-<number>-<short-description>`. Choose a meaningful name in English.
   Use `feat/` for features, `bug/` for bug fixes, and `docs/` for documentation.
   Use a suitable prefix for other changes, such as `refactor/`, `test/`, or `chore/`.
   If a worktree and branch already exist for this issue, continue there.
3. Make all changes for the issue in that worktree. Keep the changes within the issue scope and the user's instructions.
4. Review the diff and do the checks applicable to the changes.
   For code changes, run the applicable tests with `poetry run pytest`.
   For documentation-only changes, check the content, links, examples, and `git diff --check`.
   Record the checks and their results. State which checks you could not run and why.
5. Commit the changes on the issue branch. Push that branch to `origin`.
6. Open a pull request against `main`.
   Describe the changes and the checks in English. Include `Closes #<number>` to link the issue.
   Give the user the pull request URL. Do not merge the pull request unless the user requests a merge.

### Example commands

This example is for a documentation change. Use the prefix that describes your change.
Replace `123` and `short-description` with the issue number and a short description.
Select a worktree path that does not already exist.
Run the first two commands from an existing checkout of this repository.

```sh
git fetch origin main
git worktree add -b docs/issue-123-short-description ../lab-mcp-issue-123 origin/main
cd ../lab-mcp-issue-123
```

Make the changes and complete the applicable checks in the new worktree.
Add only the files for this issue, then commit and push the changes.

```sh
git add <changed-files>
git commit -m "Describe the change in English"
git push -u origin docs/issue-123-short-description
gh pr create --base main --head docs/issue-123-short-description --title "Describe the change" --body-file <pr-body-file>
```

Replace `<changed-files>` with the file paths. Replace `<pr-body-file>` with a file that contains the pull request description.
The description must include the checks and `Closes #123` for this example.
