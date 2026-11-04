# Project folder spelling

AbuDecomp64 reads project folder roots through `abu_decomp.layout`, which delegates to CakeUnbake64's `unbake.config.layout_at`. The project `config.toml` overrides the packaged layout defaults. Source and header root lists and assembly, build, ROM, version and asset roots retain their exact spelling.

Pass the project root into path consumers. Do not spell default folders in consumers or derive folder names by changing case. Compare source membership by path segments. Include lookup may use a case-insensitive index, but emitted paths always use the original tracked spelling. File suffix classification and C identifier guards are separate from folder spelling.

DataConfig derives its omitted path templates and header search flags from that project's layout. Cleanup, dependency commands, build evidence, retargeting and TAS coverage use the same reader. A profile's explicit path overrides remain explicit inputs.

Run `bin/test tests/test_layout_verbatim.py` for the AST census and mixed-case command regressions. The fixture exercises `Source/Engine/Unicol`, `Include` and `Build`, including the public cleanup command and dependency-command arguments. The ROM comparison is a separate integration proof; unit tests cannot establish byte-identical ROM output.
