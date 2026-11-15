const assert = require('node:assert/strict');
const test = require('node:test');

const finish = require('./pr-auto-finish.cjs');

const trustedPostReleasePr = {
  state: 'open',
  mergeable_state: 'clean',
  user: { login: 'markethelm[bot]' },
  head: {
    ref: 'chore/post-release-sync-1.2.3',
    sha: 'head-sha',
    repo: { full_name: 'lawaloy/markethelm' },
  },
  base: { ref: 'main', sha: 'base-sha' },
  labels: [],
};

const stickyE2eComment = {
  user: { login: 'github-actions[bot]' },
  body: '<!-- Sticky Pull Request Commente2e-smoke-PR E2E -->',
};

const codexWrapperReview = {
  id: 1,
  user: { login: 'chatgpt-codex-connector[bot]' },
  state: 'COMMENTED',
  body:
    '### 💡 Codex Review\n\n' +
    'Here are some automated review suggestions for this pull request.\n\n' +
    '**Reviewed commit:** `abc123`\n\n' +
    '<details><summary>About Codex</summary>Info</details>',
  submitted_at: '2026-01-01T00:00:00Z',
};

test('already-merged trusted PR is an idempotent success', async () => {
  const originalLane = process.env.LANE;
  const originalPullNumber = process.env.PULL_NUMBER;
  process.env.LANE = 'dependabot';
  process.env.PULL_NUMBER = '611';

  let mergeCalled = false;
  let failure = '';
  const info = [];
  try {
    await finish({
      github: {
        rest: {
          pulls: {
            get: async () => ({
              data: {
                state: 'closed',
                merged: true,
                mergeable_state: 'unknown',
                labels: [],
              },
            }),
            merge: async () => {
              mergeCalled = true;
            },
          },
        },
      },
      context: { payload: {}, repo: { owner: 'lawaloy', repo: 'markethelm' } },
      core: {
        info: (message) => info.push(message),
        setFailed: (message) => {
          failure = message;
        },
      },
    });
  } finally {
    if (originalLane === undefined) delete process.env.LANE;
    else process.env.LANE = originalLane;
    if (originalPullNumber === undefined) delete process.env.PULL_NUMBER;
    else process.env.PULL_NUMBER = originalPullNumber;
  }

  assert.equal(mergeCalled, false);
  assert.equal(failure, '');
  assert.deepEqual(info, ['PR #611 is already merged; nothing to do.']);
});

const runPostReleaseFinish = async ({ comments, reviews, threads }) => {
  const originalLane = process.env.LANE;
  const originalPullNumber = process.env.PULL_NUMBER;
  process.env.LANE = 'post-release';
  process.env.PULL_NUMBER = '605';

  let mergeCalled = false;
  let failure = '';
  const listForRef = () => {};
  const listComments = () => {};
  const listReviews = () => {};
  const github = {
    rest: {
      checks: { listForRef },
      issues: { listComments },
      repos: { compareCommits: async () => ({ data: { ahead_by: 0 } }) },
      pulls: {
        get: async () => ({ data: trustedPostReleasePr }),
        listReviews,
        merge: async () => {
          mergeCalled = true;
        },
      },
    },
    paginate: async (route) => {
      if (route === listForRef) return [];
      if (route === listComments) return comments;
      if (route === listReviews) return reviews;
      throw new Error('Unexpected pagination route');
    },
    graphql: async () => ({
      repository: {
        pullRequest: { reviewThreads: { nodes: threads } },
      },
    }),
  };
  const core = {
    info: () => {},
    setFailed: (message) => {
      failure = message;
    },
  };

  try {
    await finish({
      github,
      context: { payload: {}, repo: { owner: 'lawaloy', repo: 'markethelm' } },
      core,
    });
  } finally {
    if (originalLane === undefined) delete process.env.LANE;
    else process.env.LANE = originalLane;
    if (originalPullNumber === undefined) delete process.env.PULL_NUMBER;
    else process.env.PULL_NUMBER = originalPullNumber;
  }

  return { mergeCalled, failure };
};

test('post-release lane blocks feedback found immediately before merge', async () => {
  const { mergeCalled, failure } = await runPostReleaseFinish({
    comments: [
      {
        user: { login: 'reviewer' },
        body: 'Please address this late concern.',
      },
    ],
    reviews: [],
    threads: [],
  });

  assert.equal(mergeCalled, false);
  assert.match(failure, /feedback received before merge/);
});

test('post-release lane blocks unresolved threads with no conversation comments', async () => {
  const { mergeCalled, failure } = await runPostReleaseFinish({
    comments: [stickyE2eComment],
    reviews: [],
    threads: [{ isResolved: false }],
  });

  assert.equal(mergeCalled, false);
  assert.match(failure, /feedback received before merge/);
  assert.match(failure, /1 unresolved thread/);
});

test('post-release lane merges when only informational feedback remains', async () => {
  const { mergeCalled, failure } = await runPostReleaseFinish({
    comments: [stickyE2eComment],
    reviews: [codexWrapperReview],
    threads: [{ isResolved: true }],
  });

  assert.equal(failure, '');
  assert.equal(mergeCalled, true);
});

const runDependabotFinish = async ({ comparisons, mergeError, updateError }) => {
  const originalLane = process.env.LANE;
  const originalPullNumber = process.env.PULL_NUMBER;
  process.env.LANE = 'dependabot';
  process.env.PULL_NUMBER = '785';

  const pr = {
    state: 'open',
    mergeable_state: 'clean',
    user: { login: 'dependabot[bot]' },
    head: { ref: 'dependabot/pip/fastapi', sha: 'head-sha' },
    base: { ref: 'main', sha: 'base-sha' },
    labels: [],
  };
  const calls = { merge: 0, update: [], comments: [], labels: [], comparisons: [] };
  const listForRef = () => {};
  const listComments = () => {};
  let failure = '';
  const github = {
    rest: {
      checks: { listForRef },
      issues: {
        listComments,
        createComment: async ({ body }) => calls.comments.push(body),
        addLabels: async ({ labels }) => calls.labels.push(...labels),
      },
      repos: {
        compareCommits: async ({ base, head }) => {
          calls.comparisons.push({ base, head });
          return { data: { ahead_by: comparisons[calls.comparisons.length - 1] } };
        },
      },
      pulls: {
        get: async () => ({ data: pr }),
        merge: async () => {
          calls.merge += 1;
          if (mergeError) throw mergeError;
        },
        updateBranch: async (args) => {
          calls.update.push(args);
          if (updateError) throw updateError;
        },
      },
    },
    paginate: async (route) => {
      if (route === listForRef || route === listComments) return [];
      throw new Error('Unexpected pagination route');
    },
  };

  try {
    await finish({
      github,
      context: { payload: {}, repo: { owner: 'lawaloy', repo: 'markethelm' } },
      core: { info: () => {}, setFailed: (message) => (failure = message) },
    });
  } finally {
    if (originalLane === undefined) delete process.env.LANE;
    else process.env.LANE = originalLane;
    if (originalPullNumber === undefined) delete process.env.PULL_NUMBER;
    else process.env.PULL_NUMBER = originalPullNumber;
  }
  return { calls, failure };
};

test('updates a branch when main advanced despite a clean mergeable state', async () => {
  const { calls, failure } = await runDependabotFinish({ comparisons: [1] });

  assert.equal(failure, '');
  assert.equal(calls.merge, 0);
  assert.deepEqual(calls.comparisons, [{ base: 'head-sha', head: 'base-sha' }]);
  assert.equal(calls.update.length, 1);
  assert.equal(calls.update[0].expected_head_sha, 'head-sha');
});

test('recovers when main advances after the final comparison', async () => {
  const mergeError = {
    status: 405,
    response: {
      data: {
        message: 'Repository rule violations found\n6 of 6 required status checks are expected.',
      },
    },
  };
  const { calls, failure } = await runDependabotFinish({
    comparisons: [0, 1],
    mergeError,
  });

  assert.equal(failure, '');
  assert.equal(calls.merge, 1);
  assert.equal(calls.comparisons.length, 2);
  assert.equal(calls.update.length, 1);
  assert.deepEqual(calls.labels, []);
});

test('blocks auto-merge when updating the branch would conflict', async () => {
  const { calls, failure } = await runDependabotFinish({
    comparisons: [1],
    updateError: {
      status: 422,
      response: { data: { message: 'Update branch cannot be performed due to conflicts' } },
    },
  });

  assert.equal(failure, '');
  assert.equal(calls.merge, 0);
  assert.deepEqual(calls.labels, ['automerge-blocked']);
  assert.equal(calls.comments.length, 1);
  assert.match(calls.comments[0], /merge conflict/);
});

test('does not permanently block a PR for an unrelated branch-update error', async () => {
  const updateError = Object.assign(new Error('Resource not accessible by integration'), {
    status: 403,
  });
  await assert.rejects(
    runDependabotFinish({
      comparisons: [1],
      updateError,
    }),
    /Resource not accessible by integration/,
  );
});

test('does not treat an unrelated merge rejection as a stale-check race', async () => {
  const mergeError = Object.assign(new Error('Required review is missing'), { status: 405 });
  await assert.rejects(
    runDependabotFinish({
      comparisons: [0],
      mergeError,
    }),
    /Required review is missing/,
  );
});
