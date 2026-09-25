# CI strategy

Every change to `main` passes the full integration test suite before it lands,
whether the PR comes from a branch in this repository or from a fork. To keep
feedback fast, CI works in two stages:

* **Fast checks** run on every push to a PR. They include static analysis,
  unit tests and package builds, and they need no secrets.
* **The full suite** runs in the
  [GitHub merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue),
  after the PR is approved and queued. It includes the integration tests.

## Why integration tests wait for the merge queue

The integration tests need secrets, such as contract tokens. GitHub does not
give secrets to workflow runs for PRs from forks, so those PRs cannot run the
integration tests directly. Merge queue builds run inside this repository, so
they have access to the secrets. Code only reaches the queue after a
maintainer has reviewed and approved it, and that review is what makes it safe
to run the code with secrets.

This approach has two more benefits:

* Every PR follows the same path, whatever repository it comes from.
* The integration tests run once per PR, against the code exactly as it will
  land on `main`.

## One required check

Branch protection requires a single gate check from CI, in place of a list of
individual jobs. The gate checks the result of every other job, with rules
that depend on the stage:

* On a PR, the fast checks must pass, and the integration tests must not run.
* In the merge queue, the fast checks and the integration tests must all pass.

So a passing gate on a PR means only that the fast checks passed. The
integration tests have not run yet.

The gate uses explicit rules for each stage because GitHub treats a skipped
job as a passing required check. Without them, an integration run that was
skipped by mistake in the merge queue would look like a pass.

Because only the gate is required, jobs can be added, renamed or split
without changes to branch protection.

## Getting a PR merged

1. Open the PR, from a branch or a fork, and fix any failing fast checks.
2. Get an approval. The approval must cover your most recent push.
3. A maintainer adds the PR to the merge queue.
4. The queue tests the PR on top of `main` and any PRs ahead of it in the
   queue.
5. If the full suite passes, the PR merges. If it fails, the queue removes the
   PR and GitHub notifies the author.

## When a merge queue run fails

The queue can test several PRs together. When a group fails, GitHub tests the
PRs separately to find the one that caused the failure. A failure reported on
your PR can therefore come from a PR queued ahead of yours. Check the failed
run's logs before you change your code.

To reproduce an integration failure locally, see
[how to run integration tests](../how-to/integration_testing.md).

## Manual runs

Maintainers can start the full suite by hand on a branch of this repository,
for example to debug a failure before queuing a PR. Forks cannot start manual
runs.
