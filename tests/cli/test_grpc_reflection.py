"""Reflection/descriptor plumbing: toposort, pool build, dynamic message + enum request building.

Uses only protobuf (installed with the ``[run]`` extra via grpcio); the live reflection RPC walk is
covered by the e2e against a real oci-jj-server. If protobuf is absent, importing it here fails and
conftest's ``pytest_ignore_collect`` skips the file.
"""
from google.protobuf import descriptor_pb2

from briefcase.cli.grpc_engine import _build_pool, _build_request, _message_class, _toposort_files

_STR = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
_ENUM = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM
_OPT = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL


def _make_files():
    """A two-file set mirroring the real shape: vcs.proto imports common.proto."""
    common = descriptor_pb2.FileDescriptorProto(name="oci_jj/v1/common.proto", package="oci_jj.v1", syntax="proto3")
    commit = common.message_type.add(name="Commit")
    commit.field.add(name="commit_id", number=1, type=_STR, label=_OPT)

    vcs = descriptor_pb2.FileDescriptorProto(name="oci_jj/v1/vcs.proto", package="oci_jj.v1", syntax="proto3")
    vcs.dependency.append("oci_jj/v1/common.proto")
    dr = vcs.message_type.add(name="DiffRequest")
    depth = dr.enum_type.add(name="Depth")
    depth.value.add(name="BYTES", number=0)
    depth.value.add(name="BENCH", number=7)
    for nm, num in (("repo", 1), ("from_ref", 2), ("to_ref", 3)):
        dr.field.add(name=nm, number=num, type=_STR, label=_OPT)
    dr.field.add(name="depth", number=4, type=_ENUM, type_name=".oci_jj.v1.DiffRequest.Depth", label=_OPT)
    return {common.name: common, vcs.name: vcs}


def test_toposort_orders_dependencies_first():
    names = [f.name for f in _toposort_files(_make_files())]
    assert names.index("oci_jj/v1/common.proto") < names.index("oci_jj/v1/vcs.proto")


def test_build_pool_handles_shuffled_input():
    # Reverse insertion order: dependent before its dependency — must still build via the toposort.
    shuffled = dict(reversed(list(_make_files().items())))
    pool = _build_pool(shuffled)
    assert pool.FindMessageTypeByName("oci_jj.v1.DiffRequest") is not None


def test_message_roundtrip_and_enum_by_name():
    pool = _build_pool(_make_files())
    DiffRequest = _message_class(pool, "oci_jj.v1.DiffRequest")
    msg = _build_request(DiffRequest, {"repo": "R", "from_ref": "b", "to_ref": "c", "depth": "BENCH"})
    assert msg.depth == 7
    again = DiffRequest.FromString(msg.SerializeToString())
    assert again.repo == "R" and again.depth == 7


def test_build_request_ignores_unknown_fields():
    pool = _build_pool(_make_files())
    DiffRequest = _message_class(pool, "oci_jj.v1.DiffRequest")
    msg = _build_request(DiffRequest, {"repo": "R", "unknown_field": "x"})
    assert msg.repo == "R"
