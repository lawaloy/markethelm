const {
  describeFeedbackBlockers,
  hasFeedbackBlockers,
  inspectFeedback,
} = require('./pr-feedback.cjs');

module.exports = async ({ github, context, core }) => {
  const lane = process.env.LANE;
  const eventPullNumber = context.payload.workflow_run?.pull_requests?.[0]?.number;
  const inputPullNumber = Number(process.env.PULL_NUMBER || 0);
  let pull_number = eventPullNumber || inputPullNumber;
  const { owner, repo } = context.repo;
  const pollProfiles = {
    default: { maxAttempts: 10, delayMs: 15000 },
    // External checks can start after a repository workflow completes and may not
    // emit a workflow_run event when they finish. Keep a bounded three-minute
    // finalization window so a present Cursor run can become neutral/terminal.
    after_checks: { maxAttempts: 36, delayMs: 5000 },
  };
  const { maxAttempts, delayMs } = pollProfiles[process.env.POLL_PROFILE] || pollProfiles.default;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  if (!pull_number && context.payload.workflow_run?.head_branch) {
    const branch = context.payload.workflow_run.head_branch;
    const { data: prs } = await github.rest.pulls.list({
      owner,
      repo,
      state: 'open',
      head: `${owner}:${branch}`,
      per_page: 1,
    });
    if (prs.length > 0) {
      pull_number = prs[0].number;
      core.info(`Resolved PR #${pull_number} from head branch ${branch}.`);
    }
  }

  if (!pull_number) {
    core.info('Skipping because no pull request number was available on the workflow_run payload.');
    return;
  }

  const ensureComment = async (body) => {
    const comments = await github.paginate(github.rest.issues.listComments, {
      owner,
      repo,
      issue_number: pull_number,
      per_page: 100,
    });

    if (comments.some((comment) => comment.body === body)) {
      core.info('Skipping comment because the same comment already exists.');
      return;
    }

    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number: pull_number,
      body,
    });
  };

  const addBlockedLabel = async (labels) => {
    if (labels.includes('automerge-blocked')) {
      return;
    }

    await github.rest.issues.addLabels({
      owner,
      repo,
      issue_number: pull_number,
      labels: ['automerge-blocked'],
    });
  };

  const isTrustedDependabot = (pr) => {
    const authorLogin = pr.user?.login || '';
    const headRef = pr.head?.ref || '';
    return (
      ['dependabot[bot]', 'app/dependabot'].includes(authorLogin) &&
      headRef.startsWith('dependabot/')
    );
  };

  const isTrustedMaintenance = (pr, labels) => {
    const authorLogin = pr.user?.login || '';
    return (
      pr.head.repo?.full_name === `${owner}/${repo}` &&
      pr.base?.ref === 'main' &&
      pr.head?.ref === 'security/deps-maintenance' &&
      ['app/github-actions', 'github-actions[bot]', 'markethelm[bot]', 'app/markethelm'].includes(
        authorLogin,
      ) &&
      labels.includes('dependencies') &&
      labels.includes('security')
    );
  };

  const isTrustedPostRelease = (pr) => {
    const authorLogin = pr.user?.login || '';
    const headRef = pr.head?.ref || '';
    return (
      pr.head.repo?.full_name === `${owner}/${repo}` &&
      pr.base?.ref === 'main' &&
      /^chore\/post-release-sync-\d+\.\d+\.\d+$/.test(headRef) &&
      ['markethelm[bot]', 'app/markethelm'].includes(authorLogin)
    );
  };

  /** Check runs from this automation (avoid waiting on ourselves). */
  const isOwnAutoFinishCheck = (name) => {
    if (!name) return false;
    if (name === 'finish' || name.startsWith('finish /')) {
      return true;
    }
    return [
      'Dependabot Auto Finish',
      'Dependency Maintenance Auto Finish',
      'Post-release Auto Finish',
    ].includes(name);
  };

  // Bare "CodeQL" duplicates CodeQL Advanced (Analyze *); never wait on it.
  const isDuplicateDefaultCodeQl = (name) => name === 'CodeQL';

  const hasPendingBlockingChecks = async (headSha) => {
    const checkRunsFromPage = (response) => {
      const d = response.data;
      if (Array.isArray(d)) return d;
      if (d && Array.isArray(d.check_runs)) return d.check_runs;
      return [];
    };
    const checkRuns = await github.paginate(
      github.rest.checks.listForRef,
      {
        owner,
        repo,
        ref: headSha,
        per_page: 100,
      },
      checkRunsFromPage,
    );
    const pending = checkRuns.filter(
      (r) =>
        !isOwnAutoFinishCheck(r.name) &&
        !isDuplicateDefaultCodeQl(r.name) &&
        (r.status === 'queued' || r.status === 'in_progress'),
    );
    if (pending.length > 0) {
      core.info(
        `Waiting for ${pending.length} check run(s) still queued or in progress: ${pending.map((r) => r.name).join(', ')}`,
      );
      return true;
    }
    return false;
  };

  const isBehindBase = async (pr) => {
    const { data: comparison } = await github.rest.repos.compareCommits({
      owner,
      repo,
      base: pr.head.sha,
      head: pr.base.sha,
    });
    return comparison.ahead_by > 0;
  };

  const updateBehindBranch = async (pr) => {
    try {
      await github.rest.pulls.updateBranch({
        owner,
        repo,
        pull_number,
        expected_head_sha: pr.head.sha,
      });
      core.info(
        `Queued branch update for PR #${pull_number}; new checks must complete before merge.`,
      );
    } catch (error) {
      const latest = (await github.rest.pulls.get({ owner, repo, pull_number })).data;
      if (latest.merged || latest.state !== 'open' || latest.head.sha !== pr.head.sha) {
        core.info(`PR #${pull_number} changed while updating its branch; waiting for a fresh run.`);
        return;
      }
      const message = error.response?.data?.message || error.message || '';
      if (error.status === 422 && /conflict/i.test(message)) {
        const body =
          'Auto-merge skipped because updating this PR branch from `main` would create a merge conflict. Resolve the conflict and rerun checks before merging.';
        await ensureComment(body);
        await addBlockedLabel(latest.labels.map((label) => label.name));
        core.info(`Branch update has a merge conflict for PR #${pull_number}: ${message}`);
        return;
      }
      throw error;
    }
  };

  const requiredChecksExpected = (error) =>
    error.status === 405 &&
    /required status checks are expected/i.test(
      error.response?.data?.message || error.message || '',
    );

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const pullRequest = (
      await github.rest.pulls.get({
        owner,
        repo,
        pull_number,
      })
    ).data;
    if (pullRequest.merged === true) {
      core.info(`PR #${pull_number} is already merged; nothing to do.`);
      return;
    }
    if (pullRequest.state !== 'open') {
      core.info(`PR #${pull_number} is already closed; nothing to do.`);
      return;
    }
    const labels = pullRequest.labels.map((label) => label.name);
    const trusted =
      lane === 'dependabot'
        ? isTrustedDependabot(pullRequest)
        : lane === 'post-release'
          ? isTrustedPostRelease(pullRequest)
          : isTrustedMaintenance(pullRequest, labels);

    if (!trusted) {
      core.info(`Skipping because PR does not satisfy trusted ${lane} provenance.`);
      return;
    }

    if (labels.includes('automerge-blocked')) {
      core.info('Skipping because automerge-blocked label is present.');
      return;
    }

    core.info(`Mergeable state on attempt ${attempt}: ${pullRequest.mergeable_state}`);

    // GitHub often reports "unstable" while optional/neutral checks exist even when required checks passed.
    const canTryMerge = ['clean', 'unstable'].includes(pullRequest.mergeable_state);

    if (canTryMerge) {
      if (await hasPendingBlockingChecks(pullRequest.head.sha)) {
        if (attempt === maxAttempts) {
          core.setFailed(
            'Mergeable, but checks remained pending after the final wait. ' +
              'Retry this trusted PR with the workflow_dispatch recovery path.',
          );
          return;
        }
        await sleep(delayMs);
        continue;
      }

      const mergeCandidate = (await github.rest.pulls.get({ owner, repo, pull_number })).data;
      if (mergeCandidate.merged === true) {
        core.info(`PR #${pull_number} was merged by another run; nothing to do.`);
        return;
      }
      if (mergeCandidate.state !== 'open') {
        core.info(`PR #${pull_number} was closed by another run; nothing to do.`);
        return;
      }
      if (mergeCandidate.head.sha !== pullRequest.head.sha) {
        core.setFailed(
          'PR head changed immediately before merge. Wait for checks ' +
            'on the latest head, then use the workflow_dispatch recovery path.',
        );
        return;
      }
      if (mergeCandidate.labels.some((label) => label.name === 'automerge-blocked')) {
        core.info('Skipping because automerge-blocked was added before merge.');
        return;
      }

      // A completed check on the PR head can become stale when main advances.
      // mergeable_state may still say "clean" while GitHub recomputes its test merge.
      if (await isBehindBase(mergeCandidate)) {
        await updateBehindBranch(mergeCandidate);
        return;
      }

      if (lane === 'post-release') {
        const blockers = await inspectFeedback({
          github,
          owner,
          repo,
          pull_number,
        });
        if (hasFeedbackBlockers(blockers)) {
          core.setFailed(
            'Post-release merge blocked by feedback received before merge: ' +
              `${describeFeedbackBlockers(blockers)}.`,
          );
          return;
        }
      }

      try {
        await github.rest.pulls.merge({
          owner,
          repo,
          pull_number,
          merge_method: 'squash',
          sha: mergeCandidate.head.sha,
        });
      } catch (error) {
        if (!requiredChecksExpected(error)) throw error;

        // main may have advanced after the comparison but before the merge API call.
        const latest = (await github.rest.pulls.get({ owner, repo, pull_number })).data;
        if (latest.merged || latest.state !== 'open') {
          core.info(`PR #${pull_number} was completed by another run.`);
          return;
        }
        if (latest.head.sha !== mergeCandidate.head.sha) {
          core.info(`PR #${pull_number} head changed before merge; waiting for new checks.`);
          return;
        }
        if (latest.labels.some((label) => label.name === 'automerge-blocked')) {
          core.info('Skipping recovery because automerge-blocked was added before merge.');
          return;
        }
        if (await isBehindBase(latest)) {
          await updateBehindBranch(latest);
          return;
        }
        if (attempt === maxAttempts) {
          core.setFailed(
            'Required checks are still expected on the current PR head. ' +
              'Retry this trusted PR with the workflow_dispatch recovery path.',
          );
          return;
        }
        core.info('GitHub still expects required checks; waiting for check registration.');
        await sleep(delayMs);
        continue;
      }
      core.info(`Merged PR #${pull_number}.`);
      return;
    }

    if (pullRequest.mergeable_state === 'behind') {
      await updateBehindBranch(pullRequest);
      return;
    }

    if (attempt === maxAttempts) {
      core.setFailed(
        'PR did not become mergeable during the final wait. ' +
          'Retry this trusted PR with the workflow_dispatch recovery path.',
      );
      return;
    }

    await sleep(delayMs);
  }
};
