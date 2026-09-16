"""
Creator profiles: direct support, merch, and affiliate links.

--------------------------------------------------------------------------
Why the money never touches this server
--------------------------------------------------------------------------
A fan clicks a creator's support link and lands on that creator's own Stripe,
PayPal, Ko-fi, Patreon, or Buy Me a Coffee page. They pay the creator there.
This platform stores a URL and a label. It does not process the payment, hold
the funds, take a cut, or ever see a card number.

That is the whole design, and it is worth being explicit about the alternative,
because the alternative is the one most platforms use and it is much harder
than it looks. If this server collected fan payments into one account and then
paid creators out of it, it would be taking custody of other people's money in
transit. In the US that is money transmission, and it generally means:

  * state-by-state money transmitter licensing, with bonding and capital
    requirements per state,
  * FinCEN registration, plus KYC and AML obligations on every creator,
  * 1099-K issuance and the tax reporting that follows,
  * and liability for chargebacks and fraud on every transaction.

None of that is triggered by publishing a link to somebody else's payment page.
The direct model also removes failure modes that have nothing to do with law:
no payout queue to get stuck, no platform balance to reconcile, no PCI scope,
no chargeback exposure, and no possibility of this server losing a creator's
money — because it never had it.

It also happens to be what "direct payments from their fans" actually means. A
platform that intermediates is not paying creators directly; it is paying them
eventually, minus something.

If you later decide you do want to intermediate — a platform-wide tip jar, a
revenue split, a subscription tier billed here — that is a different product
with a compliance surface attached, and it needs an accountant and a lawyer
before it needs code. Stripe Connect is the usual route and it exists precisely
because this problem is hard. Nothing in this module blocks that path later.

None of the above is legal advice. It is the reason the code is shaped this
way, so that whoever reads it next knows the shape was chosen and not stumbled
into.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .. import db

# The four kinds of link a profile can carry.
KINDS = ("support", "merch", "affiliate", "social")

KIND_LABELS = {
    "support": "Direct support",
    "merch": "Merch",
    "affiliate": "Affiliate",
    "social": "Elsewhere",
}

# Recognised payment destinations. Purely cosmetic — it picks a badge and lets
# the editor warn when a "support" link points somewhere that takes no money.
# Anything not on this list still works; it just shows as "Other".
SUPPORT_HOSTS = {
    "stripe.com": "Stripe",
    "buy.stripe.com": "Stripe",
    "donate.stripe.com": "Stripe",
    "paypal.com": "PayPal",
    "paypal.me": "PayPal",
    "ko-fi.com": "Ko-fi",
    "patreon.com": "Patreon",
    "buymeacoffee.com": "Buy Me a Coffee",
    "liberapay.com": "Liberapay",
    "github.com": "GitHub Sponsors",
    "opencollective.com": "Open Collective",
    "cash.app": "Cash App",
    "venmo.com": "Venmo",
    "gumroad.com": "Gumroad",
    "throne.com": "Throne",
}

# Schemes that must never survive into an href. javascript: and data: in a
# user-supplied link are the classic stored-XSS vector, and this app renders
# these URLs into other people's pages.
ALLOWED_SCHEMES = {"https"}

MAX_LINKS_PER_KIND = 12


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------
def clean_url(raw: str) -> tuple[str, str | None]:
    """
    Validate a creator-supplied URL.

    Returns (url, error). HTTPS only — these links carry people to a payment
    page, and sending a fan to a plain-http checkout would be careless even if
    the destination redirects. A bare "ko-fi.com/name" gets https:// added
    rather than rejected, because that is how people actually type them.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", "Add a link."

    if "://" not in raw:
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
    except ValueError:
        return "", "That doesn't parse as a web address."

    scheme = (parsed.scheme or "").lower()
    if scheme == "http":
        return "", "Use https:// — a payment link over plain http isn't safe to hand to a fan."
    if scheme not in ALLOWED_SCHEMES:
        return "", "Links have to start with https://"
    if not parsed.netloc or "." not in parsed.netloc:
        return "", "That address is missing a domain."

    return parsed.geturl(), None


def provider_for(url: str) -> str:
    """Best-guess provider name from the host, for the badge."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    host = host[4:] if host.startswith("www.") else host
    if host in SUPPORT_HOSTS:
        return SUPPORT_HOSTS[host]
    # Match a known host one level up (e.g. someone.gumroad.com).
    for known, name in SUPPORT_HOSTS.items():
        if host.endswith(f".{known}"):
            return name
    return ""


def key_for(channel_name: str) -> str:
    return (channel_name or "").strip().lower()


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------
def get(channel_name: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM creator_profiles WHERE channel_key = ?",
        (key_for(channel_name),),
    )
    return dict(row) if row else None


def get_for_user(user_id: int) -> list[dict]:
    rows = db.query(
        "SELECT * FROM creator_profiles WHERE user_id = ? ORDER BY channel_name",
        (user_id,),
    )
    return db.rows_to_dicts(rows)


def claim(channel_name: str, user_id: int) -> dict:
    """Create the profile if it isn't there, and attach it to this account."""
    key = key_for(channel_name)
    db.execute(
        """
        INSERT INTO creator_profiles (channel_key, channel_name, user_id)
        VALUES (?, ?, ?)
        ON CONFLICT(channel_key) DO UPDATE
            SET user_id = COALESCE(creator_profiles.user_id, excluded.user_id),
                updated_at = datetime('now')
        """,
        (key, (channel_name or "").strip(), user_id),
    )
    return get(channel_name) or {}


def can_edit(channel_name: str, user: dict | None) -> bool:
    """
    Who may edit a channel's profile.

    Admins can edit any of them — someone has to be able to fix a bad link or
    take down a profile. Otherwise it's the account that claimed the channel,
    and an unclaimed channel can be claimed by the account whose own name
    matches it. That last rule is what stops one user claiming a channel
    somebody else is publishing under.
    """
    if not user:
        return False
    if user.get("is_admin"):
        return True

    profile = get(channel_name)
    if profile and profile.get("user_id"):
        return profile["user_id"] == user["id"]

    key = key_for(channel_name)
    return key in {
        key_for(user.get("username", "")),
        key_for(user.get("display_name", "")),
    }


def update(channel_name: str, **fields) -> None:
    allowed = {"tagline", "about", "support_note", "is_published", "channel_name"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return

    assignments = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE creator_profiles SET {assignments}, updated_at = datetime('now') "
        "WHERE channel_key = ?",
        (*updates.values(), key_for(channel_name)),
    )


def release(channel_name: str) -> None:
    db.execute(
        "DELETE FROM creator_profiles WHERE channel_key = ?", (key_for(channel_name),)
    )


# --------------------------------------------------------------------------
# Links
# --------------------------------------------------------------------------
def links(channel_name: str, kind: str | None = None,
          active_only: bool = True) -> list[dict]:
    clauses = ["channel_key = ?"]
    params: list = [key_for(channel_name)]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if active_only:
        clauses.append("is_active = 1")

    rows = db.query(
        f"SELECT * FROM creator_links WHERE {' AND '.join(clauses)} "
        "ORDER BY kind, position, id",
        params,
    )
    return db.rows_to_dicts(rows)


def links_by_kind(channel_name: str, active_only: bool = True) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for link in links(channel_name, active_only=active_only):
        grouped.setdefault(link["kind"], []).append(link)
    return grouped


def add_link(channel_name: str, kind: str, label: str, url: str,
             detail: str = "") -> tuple[int | None, str | None]:
    """Returns (link_id, error)."""
    kind = kind if kind in KINDS else "support"
    label = (label or "").strip()[:80]
    if not label:
        return None, "Give the link a label — that's what a fan sees on the button."

    clean, error = clean_url(url)
    if error:
        return None, error

    if len(links(channel_name, kind, active_only=False)) >= MAX_LINKS_PER_KIND:
        return None, f"That's the limit of {MAX_LINKS_PER_KIND} for this section."

    position = db.scalar(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM creator_links "
        "WHERE channel_key = ? AND kind = ?",
        (key_for(channel_name), kind),
        default=1,
    )

    link_id = db.insert(
        """
        INSERT INTO creator_links
            (channel_key, kind, provider, label, url, detail, position)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key_for(channel_name),
            kind,
            provider_for(clean),
            label,
            clean,
            (detail or "").strip()[:140],
            position,
        ),
    )
    return link_id, None


def get_link(link_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM creator_links WHERE id = ?", (link_id,))
    return dict(row) if row else None


def update_link(link_id: int, **fields) -> str | None:
    updates: dict = {}

    if "label" in fields:
        label = (fields["label"] or "").strip()[:80]
        if not label:
            return "The label can't be empty."
        updates["label"] = label

    if "url" in fields:
        clean, error = clean_url(fields["url"])
        if error:
            return error
        updates["url"] = clean
        updates["provider"] = provider_for(clean)

    if "detail" in fields:
        updates["detail"] = (fields["detail"] or "").strip()[:140]
    if "is_active" in fields:
        updates["is_active"] = int(bool(fields["is_active"]))
    if "position" in fields:
        updates["position"] = int(fields["position"])

    if not updates:
        return None

    assignments = ", ".join(f"{k} = ?" for k in updates)
    db.execute(
        f"UPDATE creator_links SET {assignments} WHERE id = ?",
        (*updates.values(), link_id),
    )
    return None


def delete_link(link_id: int) -> None:
    db.execute("DELETE FROM creator_links WHERE id = ?", (link_id,))


def record_click(link_id: int) -> None:
    """
    Bump a link's running total.

    One integer, incremented. No account id, no timestamp, no row per click —
    a creator can see that a link is getting used without anyone being able to
    reconstruct who used it.
    """
    db.execute(
        "UPDATE creator_links SET clicks = clicks + 1 WHERE id = ? AND is_active = 1",
        (link_id,),
    )


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
def public_profile(channel_name: str) -> dict | None:
    """The published profile plus its links, or None if there's nothing to show."""
    profile = get(channel_name)
    if not profile or not profile.get("is_published"):
        return None

    grouped = links_by_kind(channel_name)
    if not any(grouped.values()) and not profile.get("support_note"):
        return None

    profile["links"] = grouped
    profile["has_support"] = bool(grouped.get("support"))
    profile["has_merch"] = bool(grouped.get("merch"))
    profile["has_affiliate"] = bool(grouped.get("affiliate"))
    return profile


def summary(channel_name: str) -> dict:
    """Counts for the editor screen."""
    grouped = links_by_kind(channel_name, active_only=False)
    return {
        "counts": {kind: len(items) for kind, items in grouped.items()},
        "total_clicks": sum(
            link["clicks"] for items in grouped.values() for link in items
        ),
    }
