"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.StackUtils = void 0;
const cdk = require("aws-cdk-lib");
class StackUtils {
    static exportStack(stack, name, value, description) {
        return new cdk.CfnOutput(stack, name, {
            value,
            exportName: `${stack.stackName}-${name}`,
            description,
        });
    }
}
exports.StackUtils = StackUtils;
//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoic2hhcmVkLmpzIiwic291cmNlUm9vdCI6IiIsInNvdXJjZXMiOlsiLi4vLi4vbGliL3NoYXJlZC50cyJdLCJuYW1lcyI6W10sIm1hcHBpbmdzIjoiOzs7QUFBQSxtQ0FBbUM7QUFNbkMsTUFBYSxVQUFVO0lBQ25CLE1BQU0sQ0FBQyxXQUFXLENBQUMsS0FBZ0IsRUFBRSxJQUFZLEVBQUUsS0FBYSxFQUFFLFdBQW9CO1FBQ2xGLE9BQU8sSUFBSSxHQUFHLENBQUMsU0FBUyxDQUFDLEtBQUssRUFBRSxJQUFJLEVBQUU7WUFDbEMsS0FBSztZQUNMLFVBQVUsRUFBRSxHQUFHLEtBQUssQ0FBQyxTQUFTLElBQUksSUFBSSxFQUFFO1lBQ3hDLFdBQVc7U0FDZCxDQUFDLENBQUM7SUFDUCxDQUFDO0NBQ0o7QUFSRCxnQ0FRQyIsInNvdXJjZXNDb250ZW50IjpbImltcG9ydCAqIGFzIGNkayBmcm9tICdhd3MtY2RrLWxpYic7XG5cbmV4cG9ydCBpbnRlcmZhY2UgRW52aXJvbm1lbnRQcm9wcyB7XG4gICAgcmVhZG9ubHkgYWNjb3VudDogc3RyaW5nO1xufVxuXG5leHBvcnQgY2xhc3MgU3RhY2tVdGlscyB7XG4gICAgc3RhdGljIGV4cG9ydFN0YWNrKHN0YWNrOiBjZGsuU3RhY2ssIG5hbWU6IHN0cmluZywgdmFsdWU6IHN0cmluZywgZGVzY3JpcHRpb24/OiBzdHJpbmcpOiBjZGsuQ2ZuT3V0cHV0IHtcbiAgICAgICAgcmV0dXJuIG5ldyBjZGsuQ2ZuT3V0cHV0KHN0YWNrLCBuYW1lLCB7XG4gICAgICAgICAgICB2YWx1ZSxcbiAgICAgICAgICAgIGV4cG9ydE5hbWU6IGAke3N0YWNrLnN0YWNrTmFtZX0tJHtuYW1lfWAsXG4gICAgICAgICAgICBkZXNjcmlwdGlvbixcbiAgICAgICAgfSk7XG4gICAgfVxufVxuIl19