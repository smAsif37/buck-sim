import pytest

from buck_sim import netlist as nl


def load(files: dict[str, str | bytes], main: str, **kw):
    data = {k: (v if isinstance(v, bytes) else v.encode()) for k, v in files.items()}
    loader = nl.NetlistLoader(data, **kw)
    return loader.load_main(main)


def test_decode_variants():
    assert nl.decode_bytes("C1 a b 1\u00b5\n".encode()) == "C1 a b 1u\n"
    utf16 = "R1 a b 1k\r\n".encode("utf-16")
    assert nl.decode_bytes(utf16) == "R1 a b 1k\n"
    assert nl.decode_bytes("R1 a b 1k".encode("utf-16-le")) == "R1 a b 1k"
    assert nl.decode_bytes(b"* caf\xe9\nR1 a b 1") .startswith("* caf")  # cp1252 fallback


def test_comments_continuations_and_title():
    text = "My title line\n* comment\nR1 a b\n+ 1k ; tail comment\n\nC1 b 0 1u\n.end\n"
    parsed = load({"x.cir": text}, "x.cir")
    assert parsed.statements == ["R1 a b 1k", "C1 b 0 1u"]


def test_first_line_kept_when_directive():
    parsed = load({"x.cir": ".param a=1\nR1 a b 1k\n"}, "x.cir")
    assert parsed.statements[0] == ".param a=1"


def test_analysis_directives_dropped_with_warning():
    text = "* t\nR1 a 0 1\n.tran 1u 1m\n.meas tran x max v(a)\n.step param a 1 2 1\n.save v(a)\n.end\n"
    parsed = load({"x.cir": text}, "x.cir")
    assert parsed.statements == ["R1 a 0 1"]
    assert any(".tran" in w for w in parsed.warnings)


@pytest.mark.parametrize(
    "bad",
    [".control", ".system rm -rf /", ".python x", ".csparam x=1", ".wave out.wav 16 44100 v(a)", ".foo"],
)
def test_unsafe_directives_rejected(bad):
    # .wave is *dropped* (not unsafe); everything else must raise
    text = f"* t\nR1 a 0 1\n{bad}\n"
    if bad.startswith(".wave"):
        assert load({"x.cir": text}, "x.cir").statements == ["R1 a 0 1"]
    else:
        with pytest.raises(nl.UnsafeNetlistError):
            load({"x.cir": text}, "x.cir")


def test_file_reference_rejected():
    with pytest.raises(nl.UnsafeNetlistError):
        load({"x.cir": "* t\nV1 a 0 PWL file=/etc/passwd\n"}, "x.cir")


def test_include_resolves_by_basename_including_windows_paths():
    files = {"main.cir": '* t\n.include "C:\\Users\\me\\models\\ic.lib"\nX1 a b IC\n', "IC.LIB": ".subckt IC a b\nR1 a b 1\n.ends\n"}
    parsed = load(files, "main.cir")
    assert ".subckt IC a b" in parsed.statements
    assert parsed.statements[-1] == "X1 a b IC"


def test_unresolved_include_lists_uploaded_files():
    with pytest.raises(nl.UnresolvedIncludeError) as e:
        load({"main.cir": "* t\n.include missing.lib\n", "other.lib": "* x\n"}, "main.cir")
    assert "missing.lib" in str(e.value) and "other.lib" in str(e.value)


def test_include_never_reads_real_filesystem():
    with pytest.raises(nl.UnresolvedIncludeError):
        load({"main.cir": "* t\n.include /etc/passwd\n"}, "main.cir")
    with pytest.raises(nl.UnresolvedIncludeError):
        load({"main.cir": "* t\n.lib ../../secret.lib\n"}, "main.cir")


def test_circular_include():
    files = {"a.cir": "* t\n.include b.lib\n", "b.lib": ".include a.cir\n"}
    with pytest.raises(nl.NetlistError, match="circular"):
        load(files, "a.cir")


def test_lib_sections():
    lib = "* models\n.lib typ\n.model D1 D(Is=1e-14)\n.endl typ\n.lib fast\n.model D1 D(Is=1e-12)\n.endl fast\n.model COMMON D\n"
    main = "* t\n.lib models.lib fast\nR1 a 0 1\n"
    parsed = load({"main.cir": main, "models.lib": lib}, "main.cir")
    assert ".model D1 D(Is=1e-12)" in parsed.statements
    assert ".model D1 D(Is=1e-14)" not in parsed.statements
    # whole-file include skips sections but keeps the common part
    parsed = load({"main.cir": "* t\n.lib models.lib\n", "models.lib": lib}, "main.cir")
    assert parsed.statements == [".model COMMON D"]
    with pytest.raises(nl.NetlistError, match="section"):
        load({"main.cir": "* t\n.lib models.lib nope\n", "models.lib": lib}, "main.cir")


def test_builtin_libs_only_when_allowed():
    text = "* t\n.lib standard.dio\nD1 a 0 1N4148\n"
    with pytest.raises(nl.UnresolvedIncludeError):
        load({"x.cir": text}, "x.cir")
    parsed = load({"x.cir": text}, "x.cir", builtin_libs=frozenset({"standard.dio"}))
    assert ".lib standard.dio" in parsed.statements


def test_encrypted_model_rejected():
    files = {"m.cir": "* t\n.include enc.lib\n", "enc.lib": "$CDNENCSTART\nabcdef\n$CDNENCFINISH\n"}
    with pytest.raises(nl.NetlistError, match="encrypted"):
        load(files, "m.cir")


def test_empty_netlist():
    with pytest.raises(nl.NetlistError):
        load({"x.cir": "* only a comment\n* another\n.end\n"}, "x.cir")


def test_find_subckts_and_inspect(example_files):
    parsed = load(example_files, "buck_demo.cir")
    info = nl.inspect(parsed.statements)
    assert [s["name"] for s in info["subckts"]] == ["TOYCTRL"]
    assert info["subckts"][0]["pins"] == ["fb", "comp", "gate", "gnd"]
    assert info["voltage_sources"][0]["name"] == "V1"
    names = {x["name"] for x in info["load_candidates"]}
    assert "Rload" in names
    assert "out" in {n.lower() for n in info["nodes"]}


def test_subckt_params_not_pins():
    stmts = [".subckt X a b params: w=1 l=2", "R1 a b 1", ".ends"]
    assert nl.find_subckts(stmts)["x"].pins == ["a", "b"]
    stmts = [".subckt Y a b w=1", "R1 a b 1", ".ends"]
    assert nl.find_subckts(stmts)["y"].pins == ["a", "b"]


def test_unbalanced_subckts():
    with pytest.raises(nl.NetlistError):
        nl.find_subckts([".subckt A a b", "R1 a b 1"])
    with pytest.raises(nl.NetlistError):
        nl.find_subckts([".ends"])


def test_top_level_elements_skip_subckt_bodies():
    stmts = ["V1 in 0 1", ".subckt A a b", "V2 a b 1", ".ends", "R1 in 0 1"]
    names = [t[0] for _, t in nl.top_level_elements(stmts)]
    assert names == ["V1", "R1"]
    with pytest.raises(nl.NetlistError):
        nl.find_element(stmts, "V2")
    assert nl.find_element(stmts, "r1")[0] == 4
