# Contributing

Thanks for stopping by — but **this repository is not accepting pull requests or
contributions**.

`cronos-simulator` is internal automation that generates a synthetic engineering
fleet (`pantalasa-cronos/*`) for load-testing the [Lunar](https://earthly.dev/lunar)
demo hub at `cronos.demo.earthly.dev`. It is open-source so the workflows can run
on free public-repo GitHub Actions minutes, not because it is a community project.

If you found a bug or have a question:

- Lunar product issues belong on the [Lunar repo](https://github.com/earthly/lunar).
- Generic SDLC governance / collector / policy ideas belong on
  [earthly/lunar-lib](https://github.com/earthly/lunar-lib).
- Anything in this repo specifically — please [open an issue on `earthly/lunar`](https://github.com/earthly/lunar/issues)
  rather than here. PRs against `main` will be closed without review.

The `main` branch is protected (no force pushes, no deletions, linear history,
no required reviews). Pushes are made directly by maintainers and by the
`generate-repos` / `simulate-activity` workflows.
