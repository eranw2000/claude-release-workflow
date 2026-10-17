### 1a. Ask which reviewers to run before launching any of them

Not every repo needs all three agents. Launching `deploy-guard` at a documents repo
buys an empty report somebody has to read, and `pr-validator` at a repo with no tests
returns "return to programmer" as noise.

So before step 2, ask the user which of `code-reviewer`, `deploy-guard` and
`pr-validator` to run, and offer a recommendation read from the repo itself:
`code-reviewer` always, `deploy-guard` where the repo deploys, `pr-validator` where it
has a test suite. One message, one keystroke to accept.
