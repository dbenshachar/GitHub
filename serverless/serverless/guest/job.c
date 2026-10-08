/* Runtime v2: length-delimited UART control, one assignment then poweroff. */
#include <stdint.h>
#include "src/mmio.h"
#include "src/io.h"
#include "src/files.h"
#include "src/heap.h"
#include "src/commands.h"
#include "src/program/script.h"
static char source[90001];
static char output[1024];
static unsigned used;
static int assigned;
static void word(uint32_t n) { for(int i=0;i<4;i++) uart_put((char)(n>>(i*8))); }
static uint32_t readword(void) { uint32_t n=0; for(int i=0;i<4;i++) n|=(uint32_t)(unsigned char)uart_get()<<(i*8); return n; }
static void frame(char kind, const char *p, unsigned n) {
    uart_put(kind); word(n); for(unsigned i=0;i<n;i++) uart_put(p[i]);
}
static void flush(void) { if(used) frame('O',output,used); used=0; }
void job_printchar(char c) { if(!assigned) return; output[used++]=c; if(used==sizeof(output)) flush(); }
int job_fanout(const char *name, int count) {
    if(count<1 || count>256) return -1;
    unsigned n=0; while(name[n] && n<13) n++;
    if(!n || n>12) return -1;
    char body[17]; for(int i=0;i<4;i++) body[i]=(char)((uint32_t)count>>(8*i));
    for(unsigned i=0;i<n;i++) body[4+i]=name[i];
    flush(); frame('F',body,n+4); return 0;
}
void printint(int n) { char b[12]; unsigned i=0; unsigned v=n<0?-(unsigned)n:(unsigned)n; if(n<0) printchar('-'); do {b[i++]='0'+v%10;v/=10;}while(v);while(i)printchar(b[--i]); }
void kmain(void) {
    init_commands(); init_malloc();
    if(fs_init()) { frame('D',"1",1); return; }
    frame('R',"2",1);
    /* QMP pauses this spin before this guest is admitted into the idle pool. */
    if(uart_get()!='S') {frame('D',"1",1);return;}
    uint32_t size=readword();
    if(size>90000 || size<4) {frame('D',"1",1);return;}
    for(uint32_t i=0;i<size;i++) source[i]=uart_get();
    source[size]=0;
    /* Binary bundle: u32 main length, source, then [u8 name length,name,u32 size,source]. */
    unsigned pos=4; uint32_t main_size=0;
    for(int i=0;i<4;i++) main_size|=(uint32_t)(unsigned char)source[i]<<(8*i);
    if(main_size>60000 || main_size>size-4) {frame('D',"1",1);return;}
    pos+=main_size;
    while(pos<size) {
        unsigned n=(unsigned char)source[pos++]; char name[13];
        if(!n || n>12 || pos+n+4>size) {frame('D',"1",1);return;}
        for(unsigned i=0;i<n;i++) name[i]=source[pos++]; name[n]=0;
        uint32_t len=0;for(int i=0;i<4;i++)len|=(uint32_t)(unsigned char)source[pos++]<<(8*i);
        if(len>60000 || len>size-pos) {frame('D',"1",1);return;}
        int fd=fs_open(name,O_CREAT|O_TRUNC|O_WRONLY);
        if(fd<0 || fs_write(fd,source+pos,len)!=(long)len || fs_close(fd)<0) {frame('D',"1",1);return;}
        pos+=len;
    }
    source[4+main_size]=0; assigned=1; frame('B',"",0);
    int status=script_run(source+4); flush(); frame('D',status?"1":"0",1);
}
