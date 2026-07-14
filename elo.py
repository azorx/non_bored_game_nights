"""Elo ratings — one ladder per game.

Ratings are a *cache*, not the source of truth. `match_players.rating_before` /
`rating_after` and every row in the `ratings` table can be rebuilt from nothing
by replaying a game's matches in `played_at` order. The matches are the truth.
So every write recomputes the affected game from scratch: at game-night volumes a
full replay is a handful of rows, and replaying removes any chance of the cache
drifting from reality (which matters the day you fix a typo in an old result).

The update rule depends on the game's mode, because "who beat whom" means three
different things — this is the whole reason a game carries a mode:

  duel — classic two-player Elo. One expected-vs-actual update.
  ffa  — everyone finishes in a position. Each player is scored as the average
         of the pairwise duels they'd have had against every other player:
         beating the people below you and losing to the people above you.
  team — teams finish in a position. Teams play that same pairwise game using
         each team's mean rating, and every member moves by their team's delta.

`ffa` with two players and `duel` produce the identical number; `duel` exists so
the rest of the app (player-count rules, the sign-up label) can talk about a
strictly two-sided game.
"""

import db

# Everyone starts here — it matches the default on the `ratings` table, so a
# player with no games yet and a player explicitly seeded at 1000 are the same.
START_RATING = 1000.0

# How far a single result can move you. 32 is the classic chess K for casual
# play: responsive enough that a game night visibly changes the board, not so
# large that one fluke buries a good player.
K = 32.0


def _expected(rating_a: float, rating_b: float) -> float:
    """Probability A beats B under the logistic Elo curve. A 400-point lead is
    roughly a 10:1 favourite — the definition of the Elo scale."""
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def _outcome(placement_a: int, placement_b: int) -> float:
    """Actual score of A against B from their finishing positions. Lower number
    finishes higher, so a smaller placement wins. Equal placement is a draw."""
    if placement_a < placement_b:
        return 1.0
    if placement_a > placement_b:
        return 0.0
    return 0.5


def _match_deltas(mode: str, participants: list[dict]) -> dict:
    """Rating change for every player in one match.

    `participants` is a list of dicts with player_id, placement, team and the
    player's current `rating` going into this match. Returns {player_id: delta}.

    Everyone is bucketed into "units": in a team game a unit is a team, otherwise
    a unit is a single player. A unit rates as the mean of its members and each
    member moves by the unit's delta, so `duel` and `ffa` fall out as the special
    case where every unit has exactly one member.
    """
    if mode == "team":
        buckets: dict = {}
        for p in participants:
            # A blank team is treated as a one-person team, so a stray missing
            # label can never silently merge two people into one side.
            key = p["team"] if p["team"] else f"__solo__{p['player_id']}"
            buckets.setdefault(key, []).append(p)
        units = list(buckets.values())
    else:
        units = [[p] for p in participants]

    # (members, mean rating, best placement in the unit)
    computed = [
        (
            members,
            sum(m["rating"] for m in members) / len(members),
            min(m["placement"] for m in members),
        )
        for members in units
    ]

    others = len(computed) - 1
    deltas: dict = {}
    for members, rating, placement in computed:
        expected = 0.0
        actual = 0.0
        for o_members, o_rating, o_placement in computed:
            if o_members is members:
                continue
            expected += _expected(rating, o_rating)
            actual += _outcome(placement, o_placement)
        # Divide by the number of opponents so a six-player free-for-all can't
        # swing a rating five times as hard as a duel just for having more
        # people in the room. A lone unit (others == 0) moves nobody.
        delta = 0.0 if others <= 0 else (K / others) * (actual - expected)
        for m in members:
            deltas[m["player_id"]] = delta
    return deltas


def recompute_game(game_id) -> None:
    """Rebuild every rating for one game by replaying its matches in order.

    Runs in a single transaction: the `ratings` rows and the per-match caches
    are wiped and rewritten together, so a reader never catches the ladder
    half-updated, and a failure part-way leaves the old (consistent) numbers.
    """
    with db.transaction() as cur:
        cur.execute("select mode from games where id = %s", [game_id])
        game = cur.fetchone()
        if game is None:
            return
        mode = game["mode"]

        # Co-op games carry no Elo — their board is win rate, read straight from
        # the matches. Never write ratings for one. Clearing any that exist keeps
        # this safe even if a game was flipped to co-op after being played, and
        # makes it harmless for the delete routes to call recompute on every
        # affected game without first checking the mode.
        if mode == "coop":
            cur.execute("delete from ratings where game_id = %s", [game_id])
            return

        cur.execute(
            "select id from matches where game_id = %s "
            "order by played_at, created_at",
            [game_id],
        )
        match_ids = [row["id"] for row in cur.fetchall()]

        rating: dict = {}  # player_id -> current rating, carried across matches
        played: dict = {}  # player_id -> matches counted so far

        for match_id in match_ids:
            cur.execute(
                "select id, player_id, placement, team "
                "from match_players where match_id = %s",
                [match_id],
            )
            parts = cur.fetchall()

            participants = [
                {
                    "player_id": p["player_id"],
                    "placement": p["placement"],
                    "team": p["team"],
                    "rating": rating.get(p["player_id"], START_RATING),
                }
                for p in parts
            ]
            deltas = _match_deltas(mode, participants)

            for p in parts:
                before = rating.get(p["player_id"], START_RATING)
                after = before + deltas[p["player_id"]]
                cur.execute(
                    "update match_players "
                    "set rating_before = %s, rating_after = %s where id = %s",
                    [before, after, p["id"]],
                )
                rating[p["player_id"]] = after
                played[p["player_id"]] = played.get(p["player_id"], 0) + 1

        # Rewrite the standings for this game only. Other games are untouched.
        cur.execute("delete from ratings where game_id = %s", [game_id])
        for player_id, value in rating.items():
            cur.execute(
                "insert into ratings (player_id, game_id, rating, games_played, "
                "updated_at) values (%s, %s, %s, %s, now())",
                [player_id, game_id, value, played[player_id]],
            )
