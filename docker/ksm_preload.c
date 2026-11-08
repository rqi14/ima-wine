/* LD_PRELOAD shim: opt the process into KSM (same-page merging).
 * Wine still has a few hundred MB of identical anonymous pages per Chromium process after the
 * memory patches; KSM merges them (~0.5 GB saved with ima + a PDF open). Needs
 * /sys/kernel/mm/ksm/run=1 on the host; harmless no-op otherwise. */
#include <sys/prctl.h>
#ifndef PR_SET_MEMORY_MERGE
#define PR_SET_MEMORY_MERGE 67
#endif
__attribute__((constructor)) static void ksm_on(void) { prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0); }
