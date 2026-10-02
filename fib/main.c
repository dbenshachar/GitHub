#include <stdio.h>
#include <time.h>

int main() {
    int a, b, c;
    a = 0; b = 1;
    int res = 0;

    clock_t end = clock() + CLOCKS_PER_SEC;
    while (clock() < end) {
        c = a + b;
        b = a;
        a = c;

        res++;
    }
    printf("%d\n", res);
    return 0;
}