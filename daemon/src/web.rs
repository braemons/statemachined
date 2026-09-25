// SPDX-License-Identifier: AGPL-3.0-or-later
//! The panels, served from the same port as the rpcs.
//!
//! `client/web/` is baked in at compile time, so the binary is the whole
//! daemon and a rig needs nothing unpacked beside it. The Python daemon does
//! the same thing with package data and `web_user_interface_assets.py`.
//!
//! **`/elements/` is a contract with another repository.** A console served
//! from somewhere else imports these panels by URL, which is why CORS is open
//! here and why the path is not an implementation detail to tidy.

use axum::{
    body::Body,
    extract::Path,
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Router,
};
use rust_embed::RustEmbed;

/// `client/web/`, as it is in the repository.
#[derive(RustEmbed)]
#[folder = "../client/web/"]
#[include = "elements/*"]
#[include = "*.html"]
#[include = "*.css"]
#[include = "*.js"]
struct Assets;

pub fn router() -> Router {
    Router::new()
        .route("/", get(|| serve("index.html".to_string())))
        .route("/{*path}", get(|Path(path): Path<String>| serve(path)))
}

async fn serve(path: String) -> Response {
    // `/ui/<file>` is the published address of this daemon's own shell assets
    // and is mapped rather than dropped: the address is somebody else's, and a
    // page that 404s because a prefix was tidied away is a page nobody can
    // debug from the outside.
    let relative = path.strip_prefix("ui/").unwrap_or(&path);
    match Assets::get(relative) {
        Some(asset) => (
            StatusCode::OK,
            [(
                header::CONTENT_TYPE,
                mime_guess::from_path(relative).first_or_octet_stream().to_string(),
            )],
            Body::from(asset.data.into_owned()),
        )
            .into_response(),
        None => (StatusCode::NOT_FOUND, format!("no {relative}")).into_response(),
    }
}
