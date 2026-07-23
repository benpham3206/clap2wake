#include <IOKit/IOKitLib.h>
#include <IOKit/hidsystem/IOHIDLib.h>
#include <IOKit/hidsystem/IOHIDShared.h>
#include <IOKit/hidsystem/IOLLEvent.h>
#include <mach/mach.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

// F18 is an inert key on Ben's keyboard layout but still belongs to NX_WAKEMASK.
// Unlike CGEventPost, IOHIDPostEvent enters through IOHIDSystem's event path.
static const UInt16 kWakeKeyCode = 79;

static int post_key(io_connect_t connection, UInt32 event_type) {
    NXEventData event_data;
    memset(&event_data, 0, sizeof(event_data));
    event_data.key.keyCode = kWakeKeyCode;

    IOGPoint location = {0, 0};
    kern_return_t result = IOHIDPostEvent(
        connection,
        event_type,
        location,
        &event_data,
        kNXEventDataVersion,
        0,
        0
    );
    if (result != KERN_SUCCESS) {
        fprintf(stderr, "IOHIDPostEvent(%u) failed: 0x%x\n", event_type, result);
        return 1;
    }
    return 0;
}

int main(void) {
    IOHIDAccessType access = IOHIDCheckAccess(kIOHIDRequestTypePostEvent);
    if (access != kIOHIDAccessTypeGranted) {
        bool requested = IOHIDRequestAccess(kIOHIDRequestTypePostEvent);
        access = IOHIDCheckAccess(kIOHIDRequestTypePostEvent);
        if (!requested || access != kIOHIDAccessTypeGranted) {
            fprintf(stderr, "IOHID post-event access is not granted\n");
            return 2;
        }
    }

    io_service_t service = IOServiceGetMatchingService(
        kIOMainPortDefault,
        IOServiceMatching(kIOHIDSystemClass)
    );
    if (service == IO_OBJECT_NULL) {
        fprintf(stderr, "IOHIDSystem service was not found\n");
        return 3;
    }

    io_connect_t connection = IO_OBJECT_NULL;
    kern_return_t opened = IOServiceOpen(
        service,
        mach_task_self(),
        kIOHIDParamConnectType,
        &connection
    );
    IOObjectRelease(service);
    if (opened != KERN_SUCCESS) {
        fprintf(stderr, "IOServiceOpen(IOHIDSystem) failed: 0x%x\n", opened);
        return 4;
    }

    int result = post_key(connection, NX_KEYDOWN);
    usleep(20000);
    result |= post_key(connection, NX_KEYUP);
    IOServiceClose(connection);
    return result;
}
