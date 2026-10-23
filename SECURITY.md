# Security

lintex is a static analysis tool: it reads source files and never executes the code it lints.
The one exception is LX102, which imports **standard-library** modules in the interpreter
running lintex to check that names exist. It never imports third-party or project code.

If you find a way to make lintex execute linted code, read files it should not, or if a rule
gives a false sense of security (a dangerous pattern it claims to catch but misses), please
report it privately rather than in a public issue: open a
[GitHub security advisory](https://github.com/700799/lintex/security/advisories/new) for this
repository. You should get a response within a week.

False negatives in security rules (LX201–LX204) are welcome as public issues, since lintex
makes no claim to be a complete security scanner; pair it with a dedicated tool such as
bandit or semgrep for that.
