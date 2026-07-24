export function classify(value: boolean): string {
    if (value) {
        return "present";
    }
    return "missing";
}
