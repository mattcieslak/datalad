# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Rerun commands recorded with `datalad run`"""

__docformat__ = 'restructuredtext'


import logging
import json
import re

from datalad.interface.base import Interface
from datalad.interface.utils import eval_results
from datalad.interface.base import build_doc
from datalad.interface.results import get_status_dict
from datalad.interface.run import run_command
from datalad.interface.common_opts import save_message_opt

from datalad.support.constraints import EnsureNone, EnsureStr
from datalad.support.gitrepo import GitCommandError
from datalad.support.param import Parameter

from datalad.distribution.dataset import require_dataset
from datalad.distribution.dataset import EnsureDataset
from datalad.distribution.dataset import datasetmethod

lgr = logging.getLogger('datalad.interface.rerun')


@build_doc
class Rerun(Interface):
    """Re-execute previous `datalad run` commands.

    This will unlock any dataset content that is on record to have
    been modified by the command in the specified revision.  It will
    then re-execute the command in the recorded path (if it was inside
    the dataset). Afterwards, all modifications will be saved.
    """
    _params_ = dict(
        revision=Parameter(
            args=("revision",),
            metavar="<revision or range>",
            nargs="?",
            doc="""re-run command(s) in this revision or range.  This can be a
            commit-ish that resolves to a single commit whose command
            should be re-run. Otherwise, it is taken as a revision
            range, and all the commands that would be shown by `git
            log <range>` are re-executed.""",
            default="HEAD",
            constraints=EnsureStr()),
        dataset=Parameter(
            args=("-d", "--dataset"),
            doc="""specify the dataset from which to rerun a recorded command.
            If no dataset is given, an attempt is made to identify the dataset
            based on the current working directory. If a dataset is given,
            the command will be executed in the root directory of this
            dataset.""",
            constraints=EnsureDataset() | EnsureNone()),
        message=save_message_opt,
        branch=Parameter(
            metavar="NAME",
            args=("-b", "--branch",),
            doc="create and checkout this branch before rerunning the commands.",
            constraints=EnsureStr() | EnsureNone()),
        onto=Parameter(
            metavar="base",
            args=("--onto",),
            doc="""start point for rerunning the commands.  If not specified, commands
            are executed at HEAD.  This option can be used to specify
            an alternative start point, which will be checked out with
            the branch name specified with --branch or in a detached
            state otherwise.  As a special case, an empty value for
            this option means to use the parent of the first commit
            (e.g., 'a' for the range 'a..b' or 'a^' for the revision
            'a') as the starting point.""",
            constraints=EnsureStr() | EnsureNone()),
        root=Parameter(
            args=("--root",),
            action="store_true",
            doc="""rerun commands from all commits reachable from revision rather than
            running only the command from the revision.  This flag is
            incompatible with using a range for the revision the
            argument. In other words, run all the commands that would
            be shown by `git log <revision>`."""),
        # TODO
        # --list-commands
        #   go through the history and report any recorded command. this info
        #   could be used to unlock the associated output files for a rerun
    )

    @staticmethod
    @datasetmethod(name='rerun')
    @eval_results
    def __call__(
            revision="HEAD",
            dataset=None,
            branch=None,
            message=None,
            onto=None,
            root=False):

        ds = require_dataset(
            dataset, check_installed=True,
            purpose='rerunning a command')

        lgr.debug('rerunning command output underneath %s', ds)

        from datalad.tests.utils import ok_clean_git
        try:
            ok_clean_git(ds.path)
        except AssertionError:
            yield get_status_dict(
                'run',
                ds=ds,
                status='impossible',
                message=('unsaved modifications present, '
                         'cannot detect changes by command'))
            return

        err_info = get_status_dict('run', ds=ds)
        if not ds.repo.get_hexsha():
            yield dict(
                err_info, status='impossible',
                message='cannot re-run command, nothing recorded')
            return

        if branch and branch in ds.repo.get_branches():
            yield get_status_dict(
                "run", ds=ds, status="error",
                message="branch '{}' already exists".format(branch))
            return

        try:
            # Transform a single-commit revision into a range.  Don't
            # rely on `".." in` for the range check because it's
            # fragile (e.g., REV^- is a range).
            ds.repo.repo.git.rev_parse("--verify", "--quiet",
                                       revision + "^{commit}")
            if not root:
                revision = "{r}^..{r}".format(r=revision)
        except GitCommandError:
            # It's not a single commit.  Assume it's a range.
            if root:
                yield dict(
                    err_info, status="error",
                    message="--root is incompatible with revision range")
                return

        revs = ds.repo.repo.git.rev_list("--reverse", revision, "--").split()

        do_checkout = branch
        if onto is not None:
            if onto.strip() == "":
                ## An empty argument means go to the parent of the
                ## first revision, but that doesn't exist for --root.
                ## Instead check out an orphan branch.
                if root and branch:
                    ds.repo.checkout(branch, options=["--orphan"])
                    # Make sure we are actually on an orphan branch
                    # before doing a hard reset.
                    if ds.repo.get_hexsha():
                        yield dict(
                            err_info, status="error",
                            message="failed to create orphan branch")
                        return
                    ds.repo.repo.git.reset("--hard")
                    do_checkout = False
                elif root:
                    yield dict(
                        err_info, status="error",
                        message="branch name is required for orphan")
                    return
                else:
                    ds.repo.checkout(revs[0] + "^", options=["--detach"])
            else:
                ds.repo.checkout(onto, options=["--detach"])
        if do_checkout:
            ds.repo.checkout("HEAD", ["-b", branch])

        for rev in revs:
            # pull run info out of the revision's commit message
            try:
                rec_msg, runinfo = get_commit_runinfo(ds.repo, rev)
            except ValueError as exc:
                yield dict(
                    err_info, status='error',
                    message=str(exc)
                )
                return
            if not runinfo:
                pick = False
                try:
                    ds.repo.repo.git.merge_base("--is-ancestor", rev, "HEAD")
                except GitCommandError:  # Revision is NOT an ancestor of HEAD.
                    pick = True

                shortrev = ds.repo.repo.git.rev_parse("--short", rev)
                err_msg = "no command for {} found; {}".format(
                    shortrev,
                    "cherry picking" if pick else "skipping")
                yield dict(err_info, status='ok', message=err_msg)

                if pick:
                    ds.repo.repo.git.cherry_pick(rev)
                continue

            # now we have to find out what was modified during the
            # last run, and enable re-modification ideally, we would
            # bring back the entire state of the tree with #1424, but
            # we limit ourself to file addition/not-in-place-modification
            # for now
            for r in ds.unlock(new_or_modified(ds, rev),
                               return_type='generator', result_xfm=None):
                yield r

            for r in run_command(runinfo['cmd'], ds, rec_msg or message,
                                 rerun_info=runinfo):
                yield r


def get_commit_runinfo(repo, commit="HEAD"):
    """Return message and run record from a commit message

    If none found - returns None, None; if anything goes wrong - throws
    ValueError with the message describing the issue
    """
    commit_msg = repo.repo.git.show(commit, "--format=%s%n%n%b", "--no-patch")
    cmdrun_regex = r'\[DATALAD RUNCMD\] (.*)=== Do not change lines below ' \
                   r'===\n(.*)\n\^\^\^ Do not change lines above \^\^\^'
    runinfo = re.match(cmdrun_regex, commit_msg,
                       re.MULTILINE | re.DOTALL)
    if not runinfo:
        return None, None

    rec_msg, runinfo = runinfo.groups()

    try:
        runinfo = json.loads(runinfo)
    except Exception as e:
        raise ValueError(
            'cannot re-run command, command specification is not valid JSON: '
            '%s' % str(e)
        )
    if 'cmd' not in runinfo:
        raise ValueError(
            'cannot re-run command, command specification missing in '
            'recorded state'
        )
    return rec_msg, runinfo


def new_or_modified(dataset, revision="HEAD"):
    """Yield files that have been added or modified in `revision`.

    Parameters
    ----------
    dataset : Dataset
    revision : string, optional
        Commit-ish of interest.

    Returns
    -------
    Generator that yields AnnotatePaths instances
    """
    diff = dataset.diff(recursive=True,
                        revision="{rev}^..{rev}".format(rev=revision),
                        return_type='generator', result_renderer=None)
    for r in diff:
        if r.get('type') == 'file' and r.get('state') in ['added', 'modified']:
            r.pop('status', None)
            yield r
