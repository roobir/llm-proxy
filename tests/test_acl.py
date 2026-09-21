from app import acl


async def test_issue_and_validate_key():
    key_id, raw_token = await acl.issue_key("test-client")
    assert raw_token.startswith(f"llm_{key_id}_")
    assert await acl.validate(raw_token) == key_id


async def test_validate_rejects_unknown_token():
    assert await acl.validate("not-a-real-token") is None
    assert await acl.validate("") is None


async def test_validate_rejects_revoked_key():
    key_id, raw_token = await acl.issue_key("test-client")
    await acl.revoke_key(key_id)
    assert await acl.validate(raw_token) is None


async def test_delete_key_removes_it_entirely():
    key_id, raw_token = await acl.issue_key("test-client")
    await acl.delete_key(key_id)
    assert await acl.validate(raw_token) is None
    assert all(k["id"] != key_id for k in await acl.list_keys())


async def test_list_keys_formats_created_at_and_sorts_newest_first():
    id1, _ = await acl.issue_key("first")
    id2, _ = await acl.issue_key("second")
    keys = await acl.list_keys()
    assert [k["id"] for k in keys] == [id2, id1]
    # Human-readable, not a raw epoch float -- this is the dashboard bug fix.
    assert keys[0]["created_at"].endswith("UTC")


# --- Per-key usage -----------------------------------------------------

async def test_fresh_key_has_zeroed_usage():
    key_id, _ = await acl.issue_key("test-client")
    assert await acl.get_usage(key_id) == {
        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }


async def test_record_usage_accumulates_across_multiple_requests():
    key_id, _ = await acl.issue_key("test-client")
    await acl.record_usage(key_id, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    await acl.record_usage(key_id, {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10})
    assert await acl.get_usage(key_id) == {
        "requests": 2, "prompt_tokens": 13, "completion_tokens": 12, "total_tokens": 25,
    }


async def test_record_usage_is_isolated_per_key():
    key_a, _ = await acl.issue_key("client-a")
    key_b, _ = await acl.issue_key("client-b")
    await acl.record_usage(key_a, {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
    assert (await acl.get_usage(key_a))["requests"] == 1
    assert (await acl.get_usage(key_b))["requests"] == 0


async def test_list_keys_includes_usage():
    key_id, _ = await acl.issue_key("test-client")
    await acl.record_usage(key_id, {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    keys = await acl.list_keys()
    assert keys[0]["usage"] == {"requests": 1, "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


async def test_delete_key_also_clears_its_usage():
    key_id, _ = await acl.issue_key("test-client")
    await acl.record_usage(key_id, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    await acl.delete_key(key_id)
    # A brand-new key reusing the same id (extremely unlikely in practice,
    # but this is what the assertion actually proves) must not inherit
    # stale usage left behind by the deleted one.
    assert await acl.get_usage(key_id) == {
        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }
