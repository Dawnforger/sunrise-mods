# Mod policy

> **Draft: umceko has not reviewed this yet.**

Sunrise's registry lists mods, modpacks, resource packs, data packs and shader packs made for Sunrise. This page
says who answers for them and when one is taken down.

## Who answers for a project

A project belongs to its authors, and its entry's `owners` answer for it. Each project is under the licence its
entry names, and the registry accepts non-commercial licences (such as CC-BY-NC-SA-4.0), because listing and
installing are free. Sunrise reviews each entry before listing it, but listing is not an endorsement, and the review is
no guarantee. Source listings are reviewed only as listings (see [Source mods](#source-mods)).

## Source mods

A listing in the Nightly source list (`sources/`) is checked when it is added or changed: its format, that the
repository is public and named as GitHub names it, and that the mod's manifest is where the listing says. Its code
is not reviewed, at that commit or later ones. Players build it from source on their own computers, after the
launcher tells them so and asks for their consent. The owner of the listed repository answers for the code, and
the promises below apply to it as to any file listed here. A listing that breaks them is removed from `sources/`.

## What an author promises

When you submit a project, you promise that:

- you have the right to publish everything in it, under the licence you name;
- it does what its page says, and nothing hidden: no malware, no collecting or sending players' data they
  weren't told about, no ads or payments built into the game;
- it contains no Minecraft game files or assets, and no tables or values generated from them: a mod reads such
  values from the game at runtime through the mod API;
- it contains nothing that breaks the
  [Minecraft EULA](https://www.minecraft.net/eula) or [Usage Guidelines](https://www.minecraft.net/usage-guidelines);
- it contains nothing illegal, hateful or sexual;
- you will keep the entry accurate, and sign every version if you set a key.

## Takedowns

A file or a whole project is taken down by adding it to [`revoked.toml`](revoked.toml), with the reason. The
launcher then refuses to install it, and turns off copies already installed, showing the reason. Sunrise takes a
project down when it breaks this policy, when there is a valid legal request, or when its owners ask.

To ask for a takedown, open a [takedown issue](https://github.com/sunrise-org/sunrise-mods/issues/new?template=takedown.yml).
For a private matter, contact the Sunrise core team on [Discord](https://playsunrise.net).

This policy may change. Changes are made by pull request in this repository.
