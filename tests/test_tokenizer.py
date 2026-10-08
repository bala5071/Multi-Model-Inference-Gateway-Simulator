from __future__ import annotations

from gateway.tokenizer import Tokenizer, WordTokenizer


def test_one_token_per_whitespace_separated_word() -> None:
    tokenizer = WordTokenizer()
    assert tokenizer.encode("") == ()
    assert tokenizer.encode("   ") == ()
    assert len(tokenizer.encode("hello")) == 1
    assert len(tokenizer.encode("  a  b\tc\nd ")) == 4


def test_same_word_same_id_different_word_different_id() -> None:
    tokenizer = WordTokenizer()
    a1, b, a2 = tokenizer.encode("alpha beta alpha")
    assert a1 == a2
    assert a1 != b
    assert tokenizer.encode("Alpha") != tokenizer.encode("alpha")
    assert tokenizer.encode("alpha.") != tokenizer.encode("alpha")


def test_whitespace_differences_do_not_change_the_tokens() -> None:
    tokenizer = WordTokenizer()
    assert tokenizer.encode("a b  c") == tokenizer.encode(" a\tb\nc ")


def test_shared_text_prefix_gives_shared_token_prefix() -> None:
    tokenizer = WordTokenizer()
    system = "You are a helpful assistant."
    first = tokenizer.encode(f"{system} What is a radix tree?")
    second = tokenizer.encode(f"{system} Summarise this contract.")
    shared = len(tokenizer.encode(system))
    assert first[:shared] == second[:shared]
    assert first[shared] != second[shared]


def test_ids_are_stable_across_instances_and_processes() -> None:
    # Pinned value: fails if ids start depending on process state such as PYTHONHASHSEED.
    assert WordTokenizer().encode("hello") == WordTokenizer().encode("hello")
    assert WordTokenizer().encode("hello") == (PINNED_HELLO_ID,)


def test_word_tokenizer_satisfies_the_interface() -> None:
    tokenizer: Tokenizer = WordTokenizer()
    assert tokenizer.encode("x") == WordTokenizer().encode("x")


PINNED_HELLO_ID = 12085107955937391741
