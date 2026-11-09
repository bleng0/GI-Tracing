from gi_trace.chameleon import ch_forge, ch_hash, ch_verify, combine_shares, keygen, split_secret


def test_forge_produces_collision_without_changing_digest():
    y, x = keygen()
    digest, r, s = ch_hash(y, b"weight=120")
    r2, s2 = ch_forge(x, digest, b"weight=12")
    assert ch_verify(y, b"weight=12", digest, r2, s2)
    assert ch_verify(y, b"weight=120", digest, r, s)


def test_wrong_message_does_not_verify():
    y, _ = keygen()
    digest, r, s = ch_hash(y, b"weight=120")
    assert not ch_verify(y, b"weight=12", digest, r, s)


def test_forge_without_trapdoor_fails():
    y, x = keygen()
    digest, _, _ = ch_hash(y, b"a")
    r, s = ch_forge(x + 1, digest, b"b")
    assert not ch_verify(y, b"b", digest, r, s)


def test_any_t_shares_reconstruct_and_fewer_do_not():
    _, x = keygen()
    shares = split_secret(x, n=6, t=3)
    assert combine_shares(shares[:3]) == x
    assert combine_shares([shares[0], shares[2], shares[5]]) == x
    assert combine_shares(shares[:2]) != x
