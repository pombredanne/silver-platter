#!/usr/bin/python
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

from typing import (
    List, Optional, Tuple, Iterator, BinaryIO, Callable, Dict, Any,
    )

from breezy.branch import Branch
from breezy.diff import show_diff_trees
from breezy.errors import (
    DivergedBranches,
    PermissionDenied,
    UnrelatedBranches,
    UnsupportedOperation,
    )
from breezy.merge_directive import (
    MergeDirective,
    MergeDirective2,
    )
from breezy.trace import (
    note,
    )
from breezy.transport import Transport
from breezy.tree import Tree
from breezy.workingtree import WorkingTree
from breezy import (
    errors,
    merge as _mod_merge,
    )
from breezy.propose import (
    get_hoster,
    hosters,
    Hoster,
    MergeProposal,
    NoSuchProject,
    UnsupportedHoster,
    HosterLoginRequired,
    )

try:
    from breezy.propose import (
        iter_hoster_instances,
        SourceNotDerivedFromTarget,
        )
except ImportError:  # breezy < 3.1.1
    def iter_hoster_instances():
        for name, hoster_cls in hosters.items():
            for instance in hoster_cls.iter_instances():
                yield instance
    SourceNotDerivedFromTarget = None

import breezy.plugins.gitlab  # noqa: F401
import breezy.plugins.github  # noqa: F401
import breezy.plugins.launchpad  # noqa: F401


from .utils import (
    create_temp_sprout,
    open_branch,
    MemoryBranch,
    )


__all__ = [
    'HosterLoginRequired',
    'UnsupportedHoster',
    'PermissionDenied',
    'NoSuchProject',
    'get_hoster',
    'hosters',
    'iter_all_mps',
    ]
if SourceNotDerivedFromTarget is not None:
    __all__.append('SourceNotDerivedFromTarget')


SUPPORTED_MODES: List[str] = [
    'push',
    'attempt-push',
    'propose',
    'push-derived',
    ]


class MergeProposalDescriptionMissing(Exception):
    """No description specified for merge proposal."""


def merge_conflicts(main_branch: Branch, other_branch: Branch) -> bool:
    """Check whether two branches are conflicted when merged.

    Args:
      main_branch: Main branch to merge into
      other_branch: Branch to merge (and use for scratch access, needs write
                    access)
    Returns:
      boolean indicating whether the merge would result in conflicts
    """
    if other_branch.repository.get_graph().is_ancestor(
            main_branch.last_revision(), other_branch.last_revision()):
        return False

    other_branch.repository.fetch(
            main_branch.repository,
            revision_id=main_branch.last_revision())

    # Reset custom merge hooks, since they could make it harder to detect
    # conflicted merges that would appear on the hosting site.
    old_file_content_mergers = _mod_merge.Merger.hooks['merge_file_content']
    _mod_merge.Merger.hooks['merge_file_content'] = []
    try:
        try:
            merger = _mod_merge.Merger.from_revision_ids(
                other_branch.basis_tree(), other_branch=other_branch,
                other=main_branch.last_revision(), tree_branch=other_branch)
        except UnrelatedBranches:
            # Unrelated branches don't technically *have* to lead to
            # conflicts, but there's not a lot to be salvaged here, either.
            return True
        merger.merge_type = _mod_merge.Merge3Merger
        tree_merger = merger.make_merger()
        with tree_merger.make_preview_transform():
            return bool(tree_merger.cooked_conflicts)
    finally:
        _mod_merge.Merger.hooks['merge_file_content'] = (
                old_file_content_mergers)


class DryRunProposal(MergeProposal):
    """A merge proposal that is not actually created.

    :ivar url: URL for the merge proposal
    """

    def __init__(self, source_branch: Branch, target_branch: Branch,
                 labels: Optional[List[str]] = None,
                 description: Optional[str] = None,
                 commit_message: Optional[str] = None,
                 reviewers: Optional[List[str]] = None,
                 owner: Optional[str] = None):
        self.description = description
        self.closed = False
        self.labels = (labels or [])
        self.source_branch = source_branch
        self.target_branch = target_branch
        self.commit_message = commit_message
        self.url = None
        self.reviewers = reviewers
        self.owner = owner

    @classmethod
    def from_existing(cls, mp: MergeProposal,
                      source_branch: Optional[Branch] = None) -> MergeProposal:
        if source_branch is None:
            source_branch = open_branch(mp.get_source_branch_url())
        commit_message = mp.get_commit_message()
        return cls(
            source_branch=source_branch,
            target_branch=open_branch(mp.get_target_branch_url()),
            description=mp.get_description(),
            commit_message=commit_message)

    def __repr__(self) -> str:
        return "%s(%r, %r)" % (
            self.__class__.__name__, self.source_branch, self.target_branch)

    def get_description(self) -> Optional[str]:
        """Get the description of the merge proposal."""
        return self.description

    def set_description(self, description: str) -> None:
        self.description = description

    def get_commit_message(self) -> Optional[str]:
        return self.commit_message

    def set_commit_message(self, commit_message: str) -> None:
        self.commit_message = commit_message

    def get_source_branch_url(self) -> str:
        """Return the source branch."""
        return self.source_branch.user_url

    def get_target_branch_url(self) -> str:
        """Return the target branch."""
        return self.target_branch.user_url

    def close(self) -> None:
        """Close the merge proposal (without merging it)."""
        self.closed = True

    def is_merged(self) -> bool:
        """Check whether this merge proposal has been merged."""
        return False

    def is_closed(self) -> bool:
        """Check whether this merge proposal has been closed."""
        return False

    def reopen(self) -> None:
        pass


def push_result(
        local_branch: Branch,
        remote_branch: Branch,
        additional_colocated_branches: Optional[List[str]] = None,
        tags: Optional[List[str]] = None) -> None:
    kwargs = {}
    if tags is not None:
        kwargs['tag_selector'] = tags.__contains__
    try:
        local_branch.push(
            remote_branch, overwrite=False, **kwargs)
    except errors.LockFailed as e:
        # Almost certainly actually a PermissionDenied error..
        raise PermissionDenied(path=remote_branch.user_url, extra=e)
    for branch_name in additional_colocated_branches or []:
        try:
            add_branch = local_branch.controldir.open_branch(name=branch_name)
        except errors.NotBranchError:
            pass
        else:
            remote_branch.controldir.push_branch(
                add_branch, name=branch_name, **kwargs)


def find_existing_proposed(main_branch: Branch, hoster: Hoster, name: str,
                           overwrite_unrelated: bool = False,
                           owner: Optional[str] = None
                           ) -> Tuple[
        Optional[Branch], Optional[bool], Optional[MergeProposal]]:
    """Find an existing derived branch with the specified name, and proposal.

    Args:
      main_branch: Main branch
      hoster: The hoster
      name: Name of the derived branch
      overwrite_unrelated: Whether to overwrite existing (but unrelated)
        branches
    Returns:
      Tuple with (resume_branch, overwrite_existing, existing_proposal)
      The resume_branch is the branch to continue from; overwrite_existing
      means there is an existing branch in place that should be overwritten.
    """
    try:
        existing_branch = hoster.get_derived_branch(
            main_branch, name=name, owner=owner)
    except errors.NotBranchError:
        return (None, None, None)
    else:
        note('Branch %s already exists (branch at %s)', name,
             existing_branch.user_url)
        # If there is an open or rejected merge proposal, resume that.
        merged_proposal = None
        for mp in hoster.iter_proposals(
                existing_branch, main_branch, status='all'):
            if not mp.is_closed() and not mp.is_merged():
                return (existing_branch, False, mp)
            else:
                merged_proposal = mp
        else:
            if merged_proposal is not None:
                note('There is a proposal that has already been merged at %s.',
                     merged_proposal.url)
                return (None, True, None)
            else:
                # No related merge proposals found, but there is an existing
                # branch (perhaps for a different target branch?)
                if overwrite_unrelated:
                    return (None, True, None)
                else:
                    # TODO(jelmer): What to do in this case?
                    return (None, False, None)


class Workspace(object):
    """Workspace for creating changes to a branch.

    main_branch: The upstream branch
    resume_branch: Optional in-progress branch that we previously made changes
        on, and should ideally continue from.
    cached_branch: Branch to copy revisions from, if possible.
    local_tree: The tree the user can work in
    """

    _destroy: Optional[Callable[[], None]]
    local_tree: WorkingTree
    main_branch_revid: Optional[bytes]

    def __init__(self, main_branch: Branch,
                 resume_branch: Optional[Branch] = None,
                 cached_branch: Optional[Branch] = None,
                 additional_colocated_branches: Optional[List[str]] = None,
                 dir: Optional[str] = None,
                 path: Optional[str] = None) -> None:
        self.main_branch = main_branch
        self.main_branch_revid = None
        self.cached_branch = cached_branch
        self.resume_branch = resume_branch
        self.additional_colocated_branches = (
            additional_colocated_branches or [])
        self._destroy = None
        self._dir = dir
        self._path = path

    def __str__(self):
        if self._path is None:
            return "Workspace for %s" % self.main_branch.user_url
        else:
            return "Workspace for %s at %s" (
                self.main_branch.user_url, self._path)

    def __repr__(self):
        return (
            "%s(%r, resume_branch=%r, cached_branch=%r, "
            "additional_colocated_branches=%r, dir=%r, path=%r)" % (
                type(self).__name__, self.main_branch, self.resume_branch,
                self.cached_branch, self.additional_colocated_branches,
                self._dir, self._path))

    def __enter__(self) -> Any:
        self.local_tree, self._destroy = create_temp_sprout(
            self.cached_branch or self.resume_branch or self.main_branch,
            self.additional_colocated_branches,
            dir=self._dir, path=self._path)
        self.main_branch_revid = self.main_branch.last_revision()
        self.refreshed = False
        with self.local_tree.branch.lock_write():
            if self.cached_branch:
                self.local_tree.pull(
                    self.resume_branch or self.main_branch, overwrite=True)
            if self.resume_branch:
                try:
                    self.local_tree.pull(self.main_branch, overwrite=False)
                except DivergedBranches:
                    pass
                for branch_name in self.additional_colocated_branches:
                    try:
                        remote_colo_branch = (
                            self.main_branch.controldir.open_branch(
                                name=branch_name))
                    except (errors.NotBranchError,
                            errors.NoColocatedBranchSupport):
                        continue
                    self.local_tree.branch.controldir.push_branch(
                        name=branch_name, source=remote_colo_branch,
                        overwrite=True)
                if merge_conflicts(self.main_branch, self.local_tree.branch):
                    note('restarting branch')
                    self.local_tree.update(revision=self.main_branch_revid)
                    self.local_tree.branch.generate_revision_history(
                        self.main_branch_revid)
                    self.resume_branch = None
                    self.refreshed = True
            self.orig_revid = self.local_tree.last_revision()
        return self

    def defer_destroy(self) -> Optional[Callable[[], None]]:
        ret = self._destroy
        self._destroy = None
        return ret

    def changes_since_main(self) -> bool:
        return self.local_tree.branch.last_revision() != self.main_branch_revid

    def changes_since_resume(self) -> bool:
        return self.orig_revid != self.local_tree.branch.last_revision()

    def push(self, hoster: Optional[Hoster] = None, dry_run: bool = False,
             tags: Optional[List[str]] = None) -> None:
        if hoster is None:
            hoster = get_hoster(self.main_branch)
        return push_changes(
            self.local_tree.branch, self.main_branch, hoster=hoster,
            additional_colocated_branches=self.additional_colocated_branches,
            dry_run=dry_run, tags=tags)

    def propose(self, name: str, description: str,
                hoster: Optional[Hoster] = None,
                existing_proposal: Optional[MergeProposal] = None,
                overwrite_existing: Optional[bool] = None,
                labels: Optional[List[str]] = None,
                dry_run: bool = False,
                commit_message: Optional[str] = None,
                reviewers: Optional[List[str]] = None,
                tags: Optional[List[str]] = None,
                owner: Optional[str] = None,
                allow_collaboration: bool = False) -> MergeProposal:
        if hoster is None:
            hoster = get_hoster(self.main_branch)
        return propose_changes(
            self.local_tree.branch, self.main_branch, hoster=hoster, name=name,
            mp_description=description, resume_branch=self.resume_branch,
            resume_proposal=existing_proposal,
            overwrite_existing=(overwrite_existing or False), labels=labels,
            dry_run=dry_run, commit_message=commit_message,
            reviewers=reviewers, owner=owner,
            additional_colocated_branches=self.additional_colocated_branches,
            tags=tags, allow_collaboration=allow_collaboration)

    def push_derived(self,
                     name: str, hoster: Optional[Hoster] = None,
                     overwrite_existing: Optional[bool] = False,
                     owner: Optional[str] = None,
                     tags: Optional[List[str]] = None) -> Tuple[Branch, str]:
        """Push a derived branch.

        Args:
          name: Branch name
          hoster: Optional hoster to use
          overwrite_existing: Whether to overwrite an existing branch
          tags: Tags list to push
          owner: Owner name
        Returns:
          tuple with remote_branch and public_branch_url
        """
        if hoster is None:
            hoster = get_hoster(self.main_branch)
        return push_derived_changes(
            self.local_tree.branch,
            self.main_branch, hoster, name,
            overwrite_existing=overwrite_existing,
            owner=owner, tags=tags)

    def orig_tree(self) -> Tree:
        return self.local_tree.branch.repository.revision_tree(self.orig_revid)

    def show_diff(self, outf: BinaryIO,
                  old_label: str = 'old/', new_label: str = 'new/') -> None:
        orig_tree = self.orig_tree()
        show_diff_trees(
            orig_tree, self.local_tree.basis_tree(), outf,
            old_label=old_label, new_label=new_label)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._destroy:
            self._destroy()
            self._destroy = None
        return False


def enable_tag_pushing(branch: Branch) -> None:
    stack = branch.get_config()
    stack.set_user_option('branch.fetch_tags', True)


class PublishResult(object):
    """A object describing the result of a publish action."""

    def __init__(self, mode: str,
                 proposal: Optional[MergeProposal] = None,
                 is_new: bool = False) -> None:
        self.mode = mode
        self.proposal = proposal
        self.is_new = is_new

    def __tuple__(self) -> Tuple[Optional[MergeProposal], bool]:
        # Backwards compatibility
        return (self.proposal, self.is_new)


def publish_changes(
        ws: Workspace, mode: str, name: str,
        get_proposal_description: Callable[
            [str, Optional[MergeProposal]], Optional[str]],
        get_proposal_commit_message: Callable[
            [Optional[MergeProposal]], Optional[str]] = None,
        dry_run: bool = False,
        hoster: Optional[Hoster] = None,
        allow_create_proposal: bool = True,
        labels: Optional[List[str]] = None,
        overwrite_existing: Optional[bool] = True,
        existing_proposal: Optional[MergeProposal] = None,
        reviewers: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        derived_owner: Optional[str] = None,
        allow_collaboration: bool = False) -> PublishResult:
    """Publish a set of changes.

    Args:
      ws: Workspace to push from
      mode: Mode to use ('push', 'push-derived', 'propose')
      name: Branch name to push
      get_proposal_description: Function to retrieve proposal description
      get_proposal_commit_message: Function to retrieve proposal commit message
      dry_run: Whether to dry run
      hoster: Hoster, if known
      allow_create_proposal: Whether to allow creating proposals
      labels: Labels to set for any merge proposals
      overwrite_existing: Whether to overwrite existing (but unrelated) branch
      existing_proposal: Existing proposal to update
      reviewers: List of reviewers for merge proposal
      tags: Tags to push (None for default behaviour)
      derived_owner: Name of any derived branch
      allow_collaboration: Whether to allow target branch owners to modify
        source branch.
    """
    if mode not in SUPPORTED_MODES:
        raise ValueError("invalid mode %r" % mode)

    if not ws.changes_since_main():
        if existing_proposal is not None:
            note('closing existing merge proposal - no new revisions')
            existing_proposal.close()
        return PublishResult(mode)

    if not ws.changes_since_resume():
        # No new revisions added on this iteration, but changes since main
        # branch. We may not have gotten round to updating/creating the
        # merge proposal last time.
        note('No changes added; making sure merge proposal is up to date.')

    if hoster is None:
        hoster = get_hoster(ws.main_branch)

    if mode == 'push-derived':
        (remote_branch, public_url) = ws.push_derived(
            name=name, overwrite_existing=overwrite_existing,
            tags=tags, owner=derived_owner)
        return PublishResult(mode)

    if mode in ('push', 'attempt-push'):
        try:
            ws.push(hoster, dry_run=dry_run, tags=tags)
        except PermissionDenied:
            if mode == 'attempt-push':
                note('push access denied, falling back to propose')
                mode = 'propose'
            else:
                note('permission denied during push')
                raise
        else:
            return PublishResult(mode=mode)

    assert mode == 'propose'
    if not ws.resume_branch and not allow_create_proposal:
        # TODO(jelmer): Raise an exception of some sort here?
        return PublishResult(mode)

    mp_description = get_proposal_description(
        getattr(hoster, 'merge_proposal_description_format', 'plain'),
        existing_proposal if ws.resume_branch else None)
    if get_proposal_commit_message is not None:
        commit_message = get_proposal_commit_message(
            existing_proposal if ws.resume_branch else None)
    if not mp_description:
        raise MergeProposalDescriptionMissing()
    (proposal, is_new) = ws.propose(
        name, mp_description, hoster=hoster,
        existing_proposal=existing_proposal,
        labels=labels, dry_run=dry_run, overwrite_existing=overwrite_existing,
        commit_message=commit_message, reviewers=reviewers,
        tags=tags, allow_collaboration=allow_collaboration,
        owner=derived_owner)

    return PublishResult(mode, proposal, is_new)


def push_changes(local_branch: Branch, main_branch: Branch,
                 hoster: Hoster,
                 possible_transports: Optional[List[Transport]] = None,
                 additional_colocated_branches: Optional[List[str]] = None,
                 dry_run: bool = False, tags: Optional[List[str]] = None
                 ) -> None:
    """Push changes to a branch."""
    push_url = hoster.get_push_url(main_branch)
    note('pushing to %s', push_url)
    target_branch = open_branch(
        push_url, possible_transports=possible_transports)
    if not dry_run:
        push_result(
            local_branch, target_branch, additional_colocated_branches,
            tags=tags)


class EmptyMergeProposal(Exception):
    """Merge proposal does not have any changes."""

    def __init__(self, local_branch: Branch, main_branch: Branch):
        self.local_branch = local_branch
        self.main_branch = main_branch


def check_proposal_diff(other_branch: Branch, main_branch: Branch) -> None:
    from breezy import merge as _mod_merge
    main_revid = main_branch.last_revision()
    other_branch.repository.fetch(main_branch.repository, main_revid)
    with other_branch.lock_read():
        main_tree = other_branch.repository.revision_tree(main_revid)
        revision_graph = other_branch.repository.get_graph()
        merger = _mod_merge.Merger.from_revision_ids(
                main_tree, other_branch=other_branch,
                other=other_branch.last_revision(),
                tree_branch=MemoryBranch(
                    other_branch.repository,
                    (None, main_branch.last_revision()), None),
                revision_graph=revision_graph)
        merger.merge_type = _mod_merge.Merge3Merger
        tree_merger = merger.make_merger()
        with tree_merger.make_preview_transform() as tt:
            changes = tt.iter_changes()
            if not any(changes):
                raise EmptyMergeProposal(other_branch, main_branch)


def propose_changes(
        local_branch: Branch, main_branch: Branch,
        hoster: Hoster, name: str, mp_description: str,
        resume_branch: Optional[Branch] = None,
        resume_proposal: Optional[MergeProposal] = None,
        overwrite_existing: bool = True,
        labels: Optional[List[str]] = None,
        dry_run: bool = False, commit_message: Optional[str] = None,
        additional_colocated_branches: Optional[List[str]] = None,
        allow_empty: bool = False, reviewers: Optional[List[str]] = None,
        tags: Optional[List[str]] = None, owner: Optional[str] = None,
        allow_collaboration: bool = False) -> Tuple[MergeProposal, bool]:
    """Create or update a merge proposal.

    Args:
      local_branch: Local branch with changes to propose
      main_branch: Target branch to propose against
      hoster: Associated hoster for main branch
      mp_description: Merge proposal description
      resume_branch: Existing derived branch
      resume_proposal: Existing merge proposal to resume
      overwrite_existing: Whether to overwrite any other existing branch
      labels: Labels to add
      dry_run: Whether to just dry-run the change
      commit_message: Optional commit message
      additional_colocated_branches: Additional colocated branches to propose
      allow_empty: Whether to allow empty merge proposals
      reviewers: List of reviewers
      tags: Tags to push (None for default behaviour)
      owner: Derived branch owner
      allow_collaboration: Allow target branch owners to modify source branch
    Returns:
      Tuple with (proposal, is_new)
    """
    if not allow_empty:
        check_proposal_diff(local_branch, main_branch)
    push_kwargs = {}
    if tags is not None:
        push_kwargs['tag_selector'] = tags.__contains__
    if not dry_run:
        if resume_branch is not None:
            local_branch.push(
                resume_branch, overwrite=overwrite_existing,
                **push_kwargs)
            remote_branch = resume_branch
        else:
            remote_branch, public_branch_url = hoster.publish_derived(
                local_branch, main_branch, name=name,
                overwrite=overwrite_existing,
                owner=owner, **push_kwargs)
        for colocated_branch_name in (additional_colocated_branches or []):
            try:
                local_colo_branch = local_branch.controldir.open_branch(
                    name=colocated_branch_name)
            except errors.NotBranchError:
                pass
            else:
                remote_branch.controldir.push_branch(
                    source=local_colo_branch, overwrite=overwrite_existing,
                    name=colocated_branch_name,
                    **push_kwargs)
    if resume_proposal is not None and dry_run:
        resume_proposal = DryRunProposal.from_existing(
            resume_proposal, source_branch=local_branch)
    if (resume_proposal is not None and
            getattr(resume_proposal, 'is_closed', None) and
            resume_proposal.is_closed()):
        from breezy.propose import (
            ReopenFailed,
            )
        try:
            resume_proposal.reopen()
        except ReopenFailed:
            note('Reopening existing proposal failed. Creating new proposal.')
            resume_proposal = None
    if resume_proposal is not None:
        # Check that the proposal doesn't already has this description.
        # Setting the description (regardless of whether it changes)
        # causes Launchpad to send emails.
        if resume_proposal.get_description() != mp_description:
            resume_proposal.set_description(mp_description)
        if resume_proposal.get_commit_message() != commit_message:
            try:
                resume_proposal.set_commit_message(commit_message)
            except UnsupportedOperation:
                pass
        return (resume_proposal, False)
    else:
        if not dry_run:
            proposal_builder = hoster.get_proposer(remote_branch, main_branch)
            kwargs: Dict[str, Any] = {}
            kwargs['commit_message'] = commit_message
            kwargs['allow_collaboration'] = allow_collaboration
            try:
                mp = proposal_builder.create_proposal(
                    description=mp_description, labels=labels,
                    reviewers=reviewers, **kwargs)
            except PermissionDenied:
                note('Permission denied while trying to create '
                     'proposal.')
                raise
        else:
            mp = DryRunProposal(
                local_branch, main_branch, labels=labels,
                description=mp_description, commit_message=commit_message,
                reviewers=reviewers, owner=owner)
        return (mp, True)


def merge_directive_changes(
        local_branch: Branch, main_branch: Branch, hoster: Hoster, name: str,
        message: str, include_patch: bool = False,
        include_bundle: bool = False,
        overwrite_existing: bool = False) -> MergeDirective:
    from breezy import osutils
    import time
    remote_branch, public_branch_url = hoster.publish_derived(
        local_branch, main_branch, name=name,
        overwrite=overwrite_existing)
    public_branch = open_branch(public_branch_url)
    directive = MergeDirective2.from_objects(
        local_branch.repository, local_branch.last_revision(), time.time(),
        osutils.local_time_offset(), main_branch,
        public_branch=public_branch, include_patch=include_patch,
        include_bundle=include_bundle, message=message,
        base_revision_id=main_branch.last_revision())
    return directive


def push_derived_changes(
        local_branch: Branch, main_branch: Branch, hoster: Hoster, name: str,
        overwrite_existing: Optional[bool] = False,
        owner: Optional[str] = None,
        tags: Optional[List[str]] = None) -> Tuple[Branch, str]:
    kwargs = {}
    if tags is not None:
        kwargs['tag_selector'] = tags.__contains__
    remote_branch, public_branch_url = hoster.publish_derived(
        local_branch, main_branch, name=name, overwrite=overwrite_existing,
        owner=owner,
        **kwargs)
    return remote_branch, public_branch_url


def iter_all_mps(statuses: Optional[List[str]] = None
                 ) -> Iterator[Tuple[Hoster, MergeProposal, str]]:
    """iterate over all existing merge proposals."""
    if statuses is None:
        statuses = ['open', 'merged', 'closed']
    for instance in iter_hoster_instances():
        for status in statuses:
            try:
                for mp in instance.iter_my_proposals(status=status):
                    yield instance, mp, status
            except HosterLoginRequired:
                pass


def iter_conflicted(branch_name: str) -> Iterator[
              Tuple[str, Branch, str, Branch, Hoster, MergeProposal, bool]]:
    """Find conflicted branches owned by the current user.

    Args:
      branch_name: Branch name to search for
    """
    possible_transports: List[Transport] = []
    for hoster, mp, status in iter_all_mps(['open']):
        try:
            if mp.can_be_merged():
                continue
        except (NotImplementedError, AttributeError):
            # TODO(jelmer): Check some other way that the branch is conflicted?
            continue
        main_branch = open_branch(
            mp.get_target_branch_url(),
            possible_transports=possible_transports)
        resume_branch = open_branch(
            mp.get_source_branch_url(),
            possible_transports=possible_transports)
        if resume_branch.name != branch_name and not (
            not resume_branch.name and
                resume_branch.user_url.endswith(branch_name)):
            continue
        # TODO(jelmer): Find out somehow whether we need to modify a subpath?
        subpath = ''
        yield (resume_branch.user_url, main_branch, subpath, resume_branch,
               hoster, mp, True)
