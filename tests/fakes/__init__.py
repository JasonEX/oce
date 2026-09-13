"""In-memory test doubles shared across the unit test tree.

Each double implements only the protocol methods the code under test calls
and records what it was asked, so a test asserts on behaviour rather than
on mock call signatures.
"""
