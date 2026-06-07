"""Integration tests for CSRF enforcement on unsafe methods."""

from __future__ import annotations


def test_post_without_token_is_forbidden(admin_client):
    resp = admin_client.post("/logout")  # no x-csrf-token, no form field
    assert resp.status_code == 403


def test_post_with_header_token_succeeds(admin_client, csrf):
    token = csrf(admin_client, "/settings")
    resp = admin_client.post("/logout", headers={"x-csrf-token": token}, follow_redirects=False)
    assert resp.status_code == 303


def test_post_with_form_field_token_succeeds(admin_client, csrf):
    token = csrf(admin_client, "/settings")
    resp = admin_client.post(
        "/settings/instance",
        data={"instance_name": "Lab", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_post_with_forged_token_is_forbidden(admin_client, csrf):
    csrf(admin_client, "/settings")  # establish a real session token
    resp = admin_client.post("/logout", headers={"x-csrf-token": "forged-value"})
    assert resp.status_code == 403
