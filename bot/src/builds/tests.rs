use serde_json::{Value, json};

use super::*;

struct Recorded {
    list: Value,
    tags: BTreeMap<String, Commit>,
}

impl Releases for Recorded {
    fn releases(&self) -> Result<Vec<GithubRelease>, String> {
        serde_json::from_value(self.list.clone()).map_err(|e| e.to_string())
    }

    fn stable_commits(&self) -> Result<BTreeMap<String, Commit>, String> {
        Ok(self.tags.clone())
    }
}

#[test]
fn offered_builds_keep_every_stable_and_ten_newest_with_surfaces_per_channel() {
    let mut rows = Vec::new();
    let mut tags = BTreeMap::new();
    for i in 1..=14 {
        let tag = format!("26.3-0.1.{i}");
        rows.push(
            json!({"tag_name":tag, "target_commitish":"release/26.3", "name":"Stable",
            "published_at":"2026-11-23T00:00:00Z", "draft":false, "prerelease":false, "assets":[]}),
        );
        tags.insert(tag, Commit::parse(format!("{i:040x}")).unwrap());
        for (channel, title, sequence) in [("nightly", "Nightly", i), ("dev", "Dev", i + 100)] {
            let date = format!("2026-11-{i:02}");
            let short = format!("{sequence:07x}");
            let commit = format!("{short}{}", "0".repeat(33));
            let tag = if channel == "dev" {
                format!("dev-{date}-{short}")
            } else if i == 12 {
                "nightly".to_owned()
            } else {
                format!("nightly-{date}")
            };
            rows.push(json!({"tag_name":tag, "target_commitish":commit, "name":format!("{title} {date} · {short}"),
                "published_at":format!("{date}T00:00:00Z"), "draft":i == 13, "prerelease":true, "assets":[{"name":"sdk.json"}]}));
            rows.push(json!({"tag_name":format!("sdk-{commit}"), "target_commitish":commit, "name":"Mod SDK",
                "published_at":format!("{date}T00:00:00Z"), "draft":false, "prerelease":true,
                "assets":[{"name":if i == 14 { "sdk.json" } else { "surface.json" }}]}));
        }
    }
    // Deliberately not in newest-first order, like a GitHub list sorted by creation time.
    let selected = select(&Recorded {
        list: json!(rows),
        tags,
    })
    .unwrap();
    for (channel, offset) in [("nightly", 0), ("dev", 100)] {
        let wanted: Vec<_> = (3..=12)
            .map(|i| format!("{channel}-2026-11-{i:02}-{:07x}", i + offset))
            .collect();
        let got: Vec<_> = selected
            .iter()
            .map(|row| row.id.as_str())
            .filter(|id| id.starts_with(channel))
            .collect();
        assert_eq!(got, wanted);
    }
    assert_eq!(
        selected
            .iter()
            .filter(|row| row.id.as_str().starts_with("26.3-"))
            .count(),
        14
    );
}
