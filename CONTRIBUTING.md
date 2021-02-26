# Contributing to chaosblade

Welcome to ChaosBlade world, here is a list of contributing guide for you. If you find something incorrect or missing
 content in the page, please submit an issue or PR to fix it.


## What can you do 
Every action to make the project better is encouraged. On GitHub, every improvement for the project could be via a PR 
(short for pull request).

* If you find a typo, try to fix it!
* If you find a bug, try to fix it!
* If you find some redundant codes, try to remove them!
* If you find some test cases missing, try to add them!
* If you could enhance a feature, please **DO NOT** hesitate!
* If you find code implicit, try to add comments to make it clear!
* If you find code ugly, try to refactor that!
* If you can help to improve documents, it could not be better!
* If you find document incorrect, just do it and fix that!
* ...

Actually, it is impossible to list them completely. Just remember one principle:

**WE ARE LOOKING FORWARD TO ANY PR FROM YOU.**


## Contributing
### Preparation
Before you contribute, you need to register a Github ID. Prepare the following environment:
* go
* git

### Workflow
We use the `master` branch as the development branch, which indicates that this is an unstable branch.

Here is the workflow for contributors:

1. Fork to your own
2. Clone fork to the local repository
3. Create a new branch and work on it
4. Keep your branch in sync
5. Commit your changes (make sure your commit message concise)
6. Push your commits to your forked repository
7. Create a pull request

Please follow [Creating a pull request](https://docs.github.com/en/github/collaborating-with-issues-and-pull-requests/creating-a-pull-request).
Please make sure the PR has a corresponding issue.

After creating a PR, one or more reviewers will be assigned to the pull request.
The reviewers will review the code.

Before merging a PR, squash any fix review feedback, typo, merged, and rebased sorts of commits.
The final commit message should be clear and concise.

### Compile
Go to the project root directory which you cloned and execute compile:
```bash
make
```

If you compile the Linux package on the Mac operating system, you can do:
```bash
make build_linux
```

If you compile the chaosblade image, you can do:
```bash
make build_image
```
clean compilation:
```bash
make clean
```

### Code Style
See details of [CODE STYLE](./docs/code_styles.md)

### Commit Rules
#### Commit Message

Commit message could help reviewers better understand what is the purpose of submitted PR. It could help accelerate the code review procedure as well. We encourage contributors to use **EXPLICIT** commit message rather than an ambiguous message. In general, we advocate the following commit message type:

* feat: A new feature
* fix: A bug fix
* docs: Documentation only changes
* style: Changes that do not affect the meaning of the code (white-space, formatting, missing semi-colons, etc)
* refactor: A code change that neither fixes a bug or adds a feature
* perf: A code change that improves performance
* test: Adding missing or correcting existing tests
* chore: Changes to the build process or auxiliary tools and libraries such as documentation generation

On the other side, we discourage contributors from committing message like the following ways:

* ~~fix bug~~
* ~~update~~
* ~~add doc~~

If you get lost, please see [How to Write a Git Commit Message](http://chris.beams.io/posts/git-commit/) for a start.

#### Commit Content

Commit content represents all content changes included in one commit. We had better include things in one single commit which could support the reviewer's complete review without any other commits' help. In other word, contents in one single commit can pass the CI to avoid code mess. In brief, there are two minor rules for us to keep in mind:

* avoid very large change in a commit;
* complete and reviewable for each commit.

No matter commit message or commit content, we do take more emphasis on code review.


### Pull Request
We use [GitHub Issues](https://github.com/chaosblade-io/chaosblade/issues) and [Pull Requests](https://github.com/chaosblade-io/chaosblade/pulls) for trackers.

If you find a typo in document, find a bug in code, or want new features, or want to give suggestions,
you can [open an issue on GitHub](https://github.com/chaosblade-io/chaosblade/issues/new) to report it.
Please follow the guideline message in the issue template.

If you want to contribute, please follow the [contribution workflow](#Workflow) and create a new pull request.
If your PR contains large changes, e.g. component refactor or new components, please write detailed documents
about its design and usage.

Note that a single PR should not be too large. If heavy changes are required, it's better to separate the changes
to a few individual PRs.


### Code Review
All code should be well reviewed by one or more committers. Some principles:

- Readability: Important code should be well-documented. Comply with our code style.
- Elegance: New functions, classes or components should be well designed.
- Testability: Important code should be well-tested (high unit test coverage).

## Others
### Code of Conduct
*"In the interest of fostering an open and welcoming environment, we as contributors and maintainers pledge to make 
participation in our project and our community a harassment-free experience for everyone, regardless of age, body 
size, disability, ethnicity, sex characteristics, gender identity and expression, level of experience, education, 
socio-economic status, nationality, personal appearance, race, religion, or sexual identity and orientation..."*

See details of [CONTRIBUTOR COVENANT CODE OF CONDUCT](https://github.com/chaosblade-io/chaosblade/blob/master/CODE_OF_CONDUCT.md)

### Sign your work
The sign-off is a simple line at the end of the explanation for the patch, which certifies that you wrote it or otherwise have the right to pass it on as an open-source patch.
The rules are pretty simple: if you can certify the below (from [developercertificate.org](http://developercertificate.org/)):

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
660 York Street, Suite 102,
San Francisco, CA 94110 USA

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.

Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Then you just add a line to every git commit message:

```
Signed-off-by: Joe Smith <joe.smith@email.com>
```

Use your real name (sorry, no pseudonyms or anonymous contributions.)

If you set your `user.name` and `user.email` git configs, you can sign your commit automatically with `git commit -s`.
