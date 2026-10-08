# JAX and Apple Metal optimization research for M4

Research date: October 6, 2026.

The most practical first project is a narrowly scoped optimization in an existing open-source JAX Metal backend. For neural-network workloads, investigate a true fused matrix multiplication, bias, and activation kernel, selected by matrix shape. For simulation or reinforcement learning, investigate execution of short loop bodies without repeated CPU synchronization. Both are hypotheses to test on the intended M4; neither has a demonstrated speedup in this workspace.

Start by measuring the full JAX path. An isolated fast Metal kernel helps only if the backend can invoke it on existing buffers, preserve numerical semantics, and avoid overhead that erases the gain. The `metal-mul` directory was empty when inspected, so there is no existing implementation to extend here.

JAX combines a NumPy-like array interface with transformations such as `jit`, `grad`, and `vmap`. For this project, its key feature is that a transformed computation can become a compiled program rather than a sequence of Python tensor calls. JAX's introductory documentation explains these transformations. [JAX quickstart](https://docs.jax.dev/en/latest/quickstart.html)

The backend interface is PJRT, which manages devices, compilation, execution, and buffers. StableHLO is the intermediate representation that lets a backend consume a JAX computation without implementing the Python frontend. Apple describes its plugin as lowering JAX primitives to StableHLO, then using MPSGraph executables and Metal runtime APIs. This does not imply that a third-party backend inherits all CUDA XLA optimization passes. Its own lowering and runtime determine the generated work. [PJRT architecture](https://openxla.org/xla/pjrt), [Apple JAX Metal architecture](https://developer.apple.com/metal/jax/)

```text
JAX function and transformations
              |
          StableHLO
              |
       PJRT backend plugin
              |
    backend lowering and optimizer
              |
 MPSGraph, MLX, or custom Metal kernels
              |
       Metal GPU execution
```

MPSGraph is Apple's graph framework. MLX is Apple's array framework with a Metal implementation. A backend can map StableHLO to either, or emit its own Metal Shading Language kernels. The place to optimize depends on whether the measured cost comes from translating the graph, scheduling it, allocating buffers, copying layouts, or executing kernels.

The current backend landscape gives several starting points:

| Backend | Published architecture and status | Role in this project |
| --- | --- | --- |
| Apple `jax-metal` | Apple's experimental PJRT/MPSGraph plugin; published compatibility table still includes 0.1.0 and older JAX requirements; documented unsupported float64 and complex types | Include as a baseline only with a working version combination. Apple's page provides installation and issue links, rather than an editable backend source tree. [Apple](https://developer.apple.com/metal/jax/) |
| `jax-mps` | Open-source C++ PJRT backend now using MLX; current README targets jaxlib 0.11.x | My provisional first choice for a focused compiler or kernel contribution. [Project](https://github.com/tillahoffmann/jax-mps) |
| `metaljax` | Native PJRT plugin, StableHLO lowering, bundled modified MLX, numerous recognizers and generated kernels; beta | Strong comparison backend and source of existing techniques. Its broad optimizations increase the need to check whether a proposed contribution is already implemented. [Project](https://github.com/eterevsky/metaljax) |
| `jax-metallib` | Alpha package describing MPSGraph segments, custom Metal kernels, elementwise fusion, and a custom-kernel API; package requirements list JAX/jaxlib 0.10.x | Candidate for experimenting through an explicit kernel interface. Treat its reported operation coverage as a project claim. [Package documentation](https://pypi.org/project/jax-metallib/) |
| `MetalHLO` | Swift/C/PJRT interfaces, MPSGraph and custom-kernel paths, experimental heterogeneous execution | Useful compiler research reference. Its README warns that real O3 fusion has correctness/stability bugs and normally falls back to O2. [Project](https://github.com/pedronahum/MetalHLO) |

These names are separate packages. A backend called `mps` may execute through MLX rather than MPSGraph. The January 2026 introduction of `jax-mps` describes an MPSGraph implementation, while the current README describes MLX; use the current source when deciding where to patch. [Original announcement](https://github.com/jax-ml/jax/discussions/34648), [Current architecture](https://github.com/tillahoffmann/jax-mps)

Install each candidate in its own environment and explicitly select its backend. Record package versions and source revisions. Two plugins may both register `mps`; mixing them in one environment makes comparisons ambiguous. A compatibility flag or a successful import does not establish that compilation, gradients, and synchronization work for the target workload.

The M4 family has substantially different resource limits. Apple specifies 120 GB/s memory bandwidth for M4, 273 GB/s for M4 Pro, and up to 546 GB/s for M4 Max; GPU core counts also differ. These are advertised hardware limits, not measured bandwidth available to a kernel. A dispatch policy should distinguish the exact chip and configuration. [Apple M4 family specifications](https://www.apple.com/newsroom/2024/10/apple-introduces-m4-pro-and-m4-max/)

Unified memory allows CPU and GPU access to the same memory pool, but dependencies still require scheduling and synchronization. MLX explicitly inserts dependencies between CPU and GPU streams. A JAX backend may also allocate or copy data when importing buffers. Shared memory therefore does not make every framework boundary free. [MLX unified memory](https://raw.githubusercontent.com/ml-explore/mlx/main/docs/src/usage/unified_memory.rst)

For a rough performance model, let F be useful floating-point operations, B be bytes actually moved through the memory hierarchy, P be achievable compute throughput, and W be achievable bandwidth. GPU time is bounded below approximately by `max(F/P, B/W)`; host dispatch and synchronization add further costs. This is an analytical model, not an M4 measurement. It suggests three distinct interventions: improve compute utilization, eliminate intermediate traffic, or reduce host work.

Metal's tensor API is not evidence that M4 executes through the Neural Engine. Apple's WWDC26 material associates the GPU Neural Accelerator with M5 and A19 Pro. The independent Rigel preprint studies one M4 Max and reports that its Metal 4.1 matrix path runs on GPU shader cores, with no evidence of ANE routing. It also reports fp8 throughput at 0.94 times fp16 for the tested primitive. Do not generalize those measurements to every M4 configuration or every quantized inference kernel. [Apple TensorOps discussion](https://developer.apple.com/videos/play/wwdc2026/359/), [Rigel](https://arxiv.org/abs/2606.12765)

Metal 4 introduces tensor resources and shader-level performance primitives, but newer APIs still require checking the actual SDK, deployment target, and device. An initial M4 prototype can use existing SIMD-group matrix operations. MLX's Steel implementation provides a concrete reference using `simdgroup_matrix` and `simdgroup_multiply_accumulate`. [Apple Metal 4 overview](https://developer.apple.com/videos/play/wwdc2025/205/), [MLX matrix kernel implementation](https://github.com/ml-explore/mlx/blob/main/mlx/backend/metal/kernels/steel/gemm/mma.h)

Source inspection rules out several simplistic first contributions. At `jax-mps` revision `7fd54c63d101585197cf875d579900d8cf75bc97`, its fusion pass already recognizes bias addition, softmax, LayerNorm, and RMSNorm. Its bias pass rewrites a supported single-use `dot_general` plus trailing bias to `mps.addmm`. Adding basic matmul-plus-bias fusion would duplicate this implementation. [Fusion pass](https://github.com/tillahoffmann/jax-mps/blob/7fd54c63d101585197cf875d579900d8cf75bc97/src/pjrt_plugin/passes/mps_fusion_pass.cc), [Bias matcher](https://github.com/tillahoffmann/jax-mps/blob/7fd54c63d101585197cf875d579900d8cf75bc97/src/pjrt_plugin/passes/fuse_bias_add.cc)

The same executable implementation already attempts `mlx::core::compile`, caches the compiled function, and can use asynchronous evaluation. However, graphs with control-flow primitives have a synchronous dependency-resolution step because of reentrant evaluation hazards. The promising question is which particular workload misses compilation or incurs loop-related waits, rather than whether caching and async dispatch exist at all. [Executable implementation](https://github.com/tillahoffmann/jax-mps/blob/7fd54c63d101585197cf875d579900d8cf75bc97/src/pjrt_plugin/mlx_executable.cc)

Layout optimization also exists. At `metaljax` revision `7546eb93aa60c6570efdca95836276d03fe70a94`, the contraction emitter has a special path for certain middle-contracted operands that preserves views and uses batched matrix multiplication to avoid a copy. MLX's matrix dispatch code separately accepts common transpose layouts and copies unsupported strides. This supports investigating layout costs, while narrowing the contribution to a missing layout or an improved profitability decision. [metaljax contractions](https://github.com/eterevsky/metaljax/blob/7546eb93aa60c6570efdca95836276d03fe70a94/plugin-native/runtime/ops_linalg.cc), [MLX matmul dispatch](https://github.com/ml-explore/mlx/blob/main/mlx/backend/metal/matmul.cpp)

My proposed optimization order is:

| Priority | Experiment | Evidence required before implementation |
| --- | --- | --- |
| 1 for neural networks | Fuse matmul, bias, and activation in the GEMM epilogue | GPU trace shows the activation still reads a materialized GEMM output; combined kernel retains competitive GEMM throughput |
| 1 for simulation or RL | Compile or fuse short loop bodies and avoid host waits | Trace shows idle GPU intervals, CPU loop execution, or synchronization dominating a representative rollout |
| 2 | Improve `dot_general` layouts and kernel selection for skinny or irregular matrices | Copies, underfilled tiles, or a poor dispatch choice explain the measured gap |
| 3 | Fuse optimizer updates across parameter buffers | Many small update kernels or memory passes remain after current compilation |
| Later | Attention variants, sparse operations, or CPU/GPU co-execution | A specific unsupported or slow workload justifies the larger implementation and correctness burden |

For the neural-network experiment, target `y = GELU(x @ w + b)`. Extend a supported GEMM epilogue so each output tile receives its bias and activation before its final store. If the baseline writes the GEMM output and a later activation reads it, fusion can eliminate roughly one write/read pair, `2 * M * N * sizeof(dtype)` bytes. Any separate unfused bias pass adds further traffic. The exact saving must be derived from the observed baseline graph.

Rigel reports a 6.5–12.9% improvement from GEMM+bias+GELU fusion in its M4 Max cache-resident experiments, with diminishing benefit as compute dominates. That establishes a plausible mechanism, not a JAX result or a comparison against every optimized library. Its experiments use a particular Metal 4.1 toolchain and machine. [Rigel full study](https://arxiv.org/html/2606.12765v1)

Use `jax-mps` provisionally because its existing bias rewrite provides a narrow integration point. First compare native MLX matmul-plus-activation, compiled MLX, and the candidate fused kernel. If MLX already produces the desired fusion or the fused GEMM loses more throughput than it saves, select another shape regime or another experiment. An inventory of the top-level fusion pass alone cannot prove that activation fusion is absent downstream.

For the first kernel, restrict supported shapes and dtypes deliberately: contiguous or simple transposed FP32/FP16 matrices, trailing bias, and a precisely specified GELU variant. Preserve accumulation and output-rounding behavior within an agreed error budget. Adding GELU to a GEMM accumulator can change where rounding occurs compared with storing the intermediate first. Keep reduced-precision multiplication as a separately labeled mode.

Select the fast path by shape only after measurement. Large square GEMMs may already be compute-bound; small matrices may be launch-bound; long skinny matrices may need a different tiling strategy. Start with a small tile candidate set, and cache selected pipeline variants. Reject unsupported layouts, dimensions, precision attributes, or extra uses of the intermediate. Preserve the original lowering as the fallback.

For training, evaluate forward and backward together. JAX differentiates the original function before backend lowering, so compiler fusion can operate on the resulting forward/backward graphs. A user-visible custom primitive or opaque FFI call instead needs derivative and batching rules. A fast forward kernel may provide little training benefit if backward recomputes an expensive intermediate or requires an additional saved buffer.

For simulation and RL, use a representative batched update inside `lax.scan`, with and without returned trajectories. A static trip count, dynamic loop condition, and multiple loop-carried arrays create different requirements. A device kernel can sometimes retain loop state in registers and execute several iterations, but cross-thread dependencies, RNG, trajectory storage, and reverse-mode differentiation may prevent a simple fusion. Start with a small, fixed-trip-count arithmetic loop whose dependencies are fully understood.

General loop fusion is not an untouched area: `metaljax` describes generated Metal kernels for recurrent scans and changes to queueing and memory limits. Compare with that implementation before attempting a new runtime. A useful contribution could cover a missing scan body, improve resource-based selection, or reduce a specific synchronization boundary. [metaljax architecture and loop changes](https://github.com/eterevsky/metaljax)

CPU/GPU co-execution deserves a separate research track. The FusionML preprint reports gains for transformer prefill, but unchanged decode throughput and slower precision-matched training across its tested chips. It identifies a scheduler/materialization issue that can serialize nominally parallel work. Shared bandwidth and dispatch costs constrain the benefit. This is evidence for workload-specific experimentation, rather than a general strategy to split every matmul across CPU and GPU. [FusionML](https://arxiv.org/abs/2607.22785)

JAX's FFI is a frontend mechanism for calling external implementations; it does not automatically give a Metal plugin CUDA-like stream and buffer access. The documented GPU example uses CUDA. A Metal integration must verify the selected plugin's custom-call handling, buffer representation, command queue, completion events, and ownership. Prefer modifying an existing backend's lowering and emitter unless its public custom-kernel API demonstrably meets those requirements. [JAX FFI](https://docs.jax.dev/en/latest/ffi.html)

Pallas currently documents NVIDIA and TPU kernel backends. It is not a ready-to-use Metal kernel authoring path. Building a Metal Pallas backend would be a larger compiler project. MLX's custom Metal kernel API is useful for independently testing the shader, but calling an MLX kernel is not itself a JAX integration. [Pallas](https://docs.jax.dev/en/latest/pallas/index.html), [MLX custom kernels](https://ml-explore.github.io/mlx/build/html/dev/custom_metal_kernels.html)

The measurement campaign should make an implementation decision possible:

1. **Establish the machine and versions.** Record exact chip, GPU core count, RAM, macOS build, Xcode/Metal toolchain, Python, JAX, jaxlib, plugin version, and source commit. Confirm the selected GPU through JAX. Use one plugin per environment. Hold power mode and thermal conditions reasonably stable.
2. **Separate compilation from execution.** Lower and compile first, warm up execution and pipelines, then time repeated calls with completion waits. Report compile time independently. JAX dispatch is asynchronous, so timing only the Python call measures submission rather than completed work. [JAX benchmarking](https://docs.jax.dev/en/latest/benchmarking.html)
3. **Measure both latency and throughput.** For latency, wait after every invocation. For sustained throughput, execute a bounded sequence and wait at its end. Use dependent updates when modeling training. Keep inputs on device, and report transfer-inclusive timing separately. Avoid accidental host conversions in the timed region.
4. **Benchmark the relevant shape distribution.** Suggested initial M values are 1, 8, 32, 128, and 512; K/N pairs include 256/256, 768/3072, 1024/4096, and an irregular case such as 1000/1536. Include transpose views, batches, and dtype variants. These are proposed test inputs, not measured bottlenecks.
5. **Compare progressively.** Measure JAX CPU, the unmodified selected Metal plugin, native MLX with and without compilation, and the modified JAX plugin. Compare other JAX Metal backends on a compatible subset. Match dtype, precision, inputs, and mathematical workload; include library initialization and compile costs only in explicitly labeled startup tests.
6. **Profile before assigning the cause.** Inspect kernel counts, buffer copies, CPU encoding time, GPU idle gaps, command-buffer boundaries, and memory allocations. Use Instruments Metal System Trace and Xcode Metal capture to inspect actual dispatches and resources. Instrument the plugin when JAX-level profiling lacks enough detail. [Apple Metal tools](https://developer.apple.com/metal/tools/)
7. **Validate semantics before publishing timing.** Compare outputs and gradients with a CPU reference using the same quantized inputs when appropriate. Test odd sizes, zero sizes, broadcast biases, transposes, cancellation, and relevant non-finite values. Check the chosen GELU approximation and precision attributes. Preserve exact integer and RNG semantics where applicable.
8. **Require a full-workload result.** Show an MLP forward pass and training step for the GEMM experiment, or a batched rollout for the loop experiment. Report median and tail latency, repeated runs, peak memory, compile cost, and unsupported cases. An initial acceptance target could be a repeatable 10% full-workload improvement on the selected regime, with fallbacks preventing material regressions elsewhere. This target is a project criterion, not a promised result.

For results, store rows containing `chip, os, backend, versions, commit, shape, layout, dtype, precision, compile_ms, median_ms, p95_ms, peak_memory, error, validation_status`. Keep the baseline revision and change together so the improvement can be reproduced.

End-to-end benefit depends on how much time the optimized region occupies. If a region takes 40% of execution and becomes twice as fast, the whole program improves by `1 / (0.60 + 0.40 / 2) = 1.25` times. This calculation is illustrative; a region's measured share determines the actual ceiling. It prevents interpreting a large microbenchmark gain as an equally large model gain.

The public benchmarks are useful leads, but not a ranking. `jax-mps` reports approximately 3.7 times JAX CPU performance for one ResNet18 training example on an M4 MacBook Air. `MetalHLO` reports other campaigns on M1 and M5 Pro, and `metaljax` publishes its own model battery. Hardware, precision, batch size, compilation, and workloads differ. Reproduce the target workload on the intended M4 before selecting a backend on performance grounds. [jax-mps example](https://github.com/tillahoffmann/jax-mps), [MetalHLO benchmarks and limitations](https://github.com/pedronahum/MetalHLO), [metaljax benchmark ledger](https://github.com/eterevsky/metaljax/blob/main/models.md)

The concrete next deliverable is a benchmark and Metal trace for a JAX MLP on the intended M4, followed by one fused epilogue prototype if the trace confirms avoidable activation traffic. For an RL or simulation workload, substitute a representative batched scan and use synchronization evidence to decide whether loop fusion is the better first contribution. Keep the optimization narrow enough that its correctness, mechanism, and useful shape range can all be demonstrated.
