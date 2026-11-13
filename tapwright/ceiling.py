"""The daemon's ceiling (docs/AGENT-TOOLS.md §18.10): what `tapwright serve` was started with is the most any call may
have. With the thin call the model writes the command line, so a call may only narrow the daemon's scope and policy:
a smaller `--apps`, more words that need an OK or are blocked. A daemon started with none of the flags has no ceiling
and behaves as it always did, which keeps older clients working. Pure logic: no phone, no network."""
from dataclasses import dataclass

from .policy import Policy


@dataclass(frozen=True)
class Ceiling:
    apps: tuple[str, ...] = ()  # `serve --apps`: bundle ids or exact names; a call may name these or fewer
    dedicated: bool = False  # `serve --dedicated`: the phone is kept for testing, so a call may name any app
    policy: Policy | None = None  # `serve --policy FILE`: the words a call's own policy may only add to
    no_dialog: bool = False  # `serve --no-dialog`: the host's dialog is never asked, whatever a call says (dialog.py)
    accounts: str | None = None  # `serve --accounts FILE`: the test accounts file (§18.6); a call can't name another
    brief: bool = False  # `serve --brief`: answers are brief unless a call says `brief: false` (§13.4); not a limit
    maps: str | None = None  # `serve --maps DIR`: where the per-app map folders are (§18.9); a call can't move it

    @property
    def is_set(self) -> bool:
        return bool(self.apps or self.dedicated or self.policy is not None or self.no_dialog or self.accounts
                    or self.brief or self.maps)

    def check_maps(self, sent: str | None) -> tuple[str | None, str | None]:
        """(the maps root this call uses, why it is refused). With no daemon root: the call's own, else none (the tool's
        home). With one: a call that sends none uses it, and one that sends another is refused."""
        if self.maps is None:
            return sent, None
        if sent is None or sent == self.maps:
            return self.maps, None
        return None, (f"over_ceiling: this daemon was started with --maps {self.maps}, so a call can't move the maps "
                      f"folder (it asked for {sent}). Restart tapwright serve with another --maps to change it.")

    def check_accounts(self, sent: str | None, thin_call: bool) -> tuple[str | None, tuple[str, str] | None]:
        """(the accounts file this call uses, (code, why) it is refused). With none sent: serve's, if it has one. A thin
        call may only name the file serve was started with (it can't widen anything: "--accounts is set when tapwright
        serve starts, not per call"); the MCP server may send its own where serve has none, and never another than serve's."""
        if sent is None:
            return self.accounts, None
        if sent == self.accounts:
            return sent, None
        if thin_call:
            return None, ("bad_params", "--accounts is set when tapwright serve starts, not per call")
        if self.accounts is None:
            return sent, None
        return None, ("not_allowed", f"over_ceiling: this daemon was started with --accounts {self.accounts}, so a call can't "
                                     f"use another file (it asked for {sent}). Restart tapwright serve to change it.")

    def refuse_scope(self, apps: tuple[str, ...], dedicated: bool) -> str | None:
        """Why a call's scope is wider than the daemon's, or None. Only an `--apps` ceiling limits scope: a dedicated
        daemon has no limit, and one with no ceiling is as before."""
        if not self.apps:
            return None
        listed = ",".join(self.apps)
        if dedicated:
            return (f"over_ceiling: this daemon was started with --apps {listed}, so a call can't ask for dedicated mode "
                    "(every app); a call may only narrow that list. Restart tapwright serve with --dedicated to change it.")
        outside = [a for a in apps if a not in self.apps]
        if outside:
            return (f"over_ceiling: this daemon was started with --apps {listed}, so a call may only narrow that list "
                    f"(name those apps or fewer), and this one named {', '.join(outside)}. Restart tapwright serve with a wider --apps to "
                    "change it.")
        return None

    def check_policy(self, sent: Policy | None) -> tuple[Policy | None, str | None]:
        """(the policy this call runs under, why it is refused). With no daemon policy: the call's own, as before. With
        one: a call that sends none runs under the daemon's; one that sends its own may only add words to it."""
        if self.policy is None:
            return sent, None
        if sent is None:
            return self.policy, None
        gone = removed_words(self.policy, sent)
        if gone:
            return None, (f"over_ceiling: this daemon's policy (tapwright serve --policy) names words, and a call's policy may "
                          f"add words but never remove one; this one leaves out {'; '.join(gone)}.")
        return sent, None


def _norm(words: tuple[str, ...]) -> set[str]:
    return {" ".join(w.split()).casefold() for w in words}


def removed_words(floor: Policy, call: Policy) -> list[str]:
    """What `call` leaves out of `floor`, as `confirm "Buy"` or `block "Wipe" (for com.shop)`. Compared for the words in
    force in each app either policy names, and in all the others (the global lists): an app's own list replaces the
    global one, so that is where a word could be dropped. A word the floor only confirms is kept if the call blocks it,
    which is stricter."""
    gone: list[str] = []
    for bundle in dict.fromkeys([None, *floor.apps, *call.apps]):
        floor_block, floor_confirm = floor.words(bundle)
        call_block, call_confirm = call.words(bundle)
        have_block = _norm(call_block)
        where = f" (for {bundle})" if bundle else ""
        gone += [f'block "{w}"{where}' for w in floor_block if " ".join(w.split()).casefold() not in have_block]
        have_confirm = _norm(call_confirm) | have_block
        gone += [f'confirm "{w}"{where}' for w in floor_confirm if " ".join(w.split()).casefold() not in have_confirm]
    return gone
