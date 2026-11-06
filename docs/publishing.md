# Publishing this independent package

Use the prepared repository folder only. Do not initialise/push from the active simulation workspace, and do not add its Git history, data, cache or result directories.

## Create a new GitHub repository

Create an empty repository on GitHub, choose the owner/name and intended visibility, and leave the automatic README, .gitignore and licence options off when importing this prepared tree. These files/settings should be reviewed in the prepared package first. A licence is still pending; see LICENSE_STATUS.md.

From a terminal **inside this extracted repository folder**, after reviewing the files:

```bash
git init -b main
git add README.md CHANGELOG.md CONTRIBUTING.md LICENSE_STATUS.md requirements.txt .gitignore .github analysis configs docs drt_pipeline examples provenance scripts tests
git status --short
git diff --cached --stat
```

The first command creates a new local Git history. The second stages only the named code/documentation paths. The last two display what will be included. Confirm that real inputs, DBs and generated outputs are absent.

Then create the local commit and connect the new remote (replace OWNER and REPOSITORY):

```bash
git commit -m "Prepare Runtime-v2 DRT research package"
git remote add origin git@github.com:OWNER/REPOSITORY.git
git push -u origin main
```

`push` uploads this new repository. GitHub authentication must already be configured on the computer/server running the command. If Git asks for author details, configure your actual name/email locally in this new repository. Do not use a force push. If a remote or local Git history already exists, inspect it before repeating setup commands.

For a private repository, add the intended collaborators through repository access settings. For a public repository, confirm the sharing scope and add the chosen licence before advertising unrestricted reuse. A successful local preparation is not evidence that anything has been published.

Official reference: [Creating a new repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository).
