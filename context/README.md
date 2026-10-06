# Context

Background Claude needs to make good product decisions.

- [product.md](product.md): what the product does and for whom. Safe to commit.
- `private/`: personal context about the candidate. Git-ignored and blocked by the
  privacy hook. Start from [about-me.example.md](about-me.example.md): copy it to
  `private/about-me.md` and fill it in.

The candidate's profile, resume, experience bank, and writing samples stay in `profile/`
(see the README); `private/` is for preferences and goals that are not part of those.
